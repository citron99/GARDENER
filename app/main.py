from contextlib import asynccontextmanager
from datetime import datetime, time as datetime_time, timedelta, timezone
import hmac
import json
import logging
from pathlib import Path
from uuid import uuid4
from time import perf_counter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from app.ai import create_ai_gateway
from app.auth import (create_token_pair, get_admin_user, get_current_user, hash_password,
                      revoke_refresh_token, rotate_refresh_token, verify_password)
from app.config import settings, validate_runtime_settings
from app.i18n import normalize_language, translate_http_error
from app.catalog import public_router as public_catalog_router, router as catalog_router
from app.billing import router as billing_router
from app.telegram import router as telegram_router
from app.database import engine, get_db
from app.models import (AIRequestLog, AdminAuditLog, AuthSession, CareEvent, Diagnosis, DiagnosisAnswer, DiagnosisFeedback,
                        DiagnosisJob, DiagnosisQuestion, DiagnosisRevision, Garden, Plant, PlantPhoto,
                        KnowledgeSourceRecord, Partner, PartnerInvoice, Product, ProductLead, Reminder, Subscription,
                        TelegramAccount, TelegramUpdate, User, UserNotification)
from app.schemas import (AdminAIRequestRead, AdminAuditLogRead, AdminJobSummaryRead, AdminOverviewRead,
                         AdminUserSummaryRead, CalendarItemRead, CareEventCreate, CareEventRead, CareEventUpdate,
                         DiagnosisAnswersCreate,
                         DiagnosisCreate, DiagnosisFeedbackCreate, DiagnosisJobRead,
                         DiagnosisFeedbackRead, DiagnosisQuestionRead, DiagnosisRead,
                         GardenCreate, GardenRead, GardenUpdate, LoginRequest,
                         KnowledgeSourceAdminRead, KnowledgeSourceUpsert, KnowledgeSyncRead,
                         NotificationRead, PhotoRead, PlantCreate, PlantHistoryRead, PlantRead, PlantUpdate,
                         ReminderCreate, ReminderRead, ReminderUpdate,
                         AccountDeleteRequest, ActionTokenRequest, EmailVerificationRequest,
                         PasswordResetConfirm, RefreshTokenRequest, TokenRead, UserProfileUpdate,
                         UserRead, UserRegister, WeatherForecastRead)
from app.services.image_service import ImageValidationError, SanitizedImage, validate_and_sanitize_image
from app.services.knowledge_service import KnowledgeSource, create_embedding_provider, sync_builtin_knowledge, upsert_knowledge_source
from app.services.seasonal_service import seasonal_calendar_items
from app.services.weather_service import WeatherServiceError, weather_service
from app.services.rate_limit_service import enforce_auth_rate_limit, enforce_rate_limit
from app.services.storage_service import (StorageError, delete_photo, photo_download,
                                          store_photo)
from app.services.storage_outbox_service import enqueue_photo_deletions, process_storage_deletion_outbox
from app.services.account_deletion_service import (
    mark_cancellation_requested, mark_cancellation_retry, stage_account_deletion,
)
from app.services.billing_service import BillingProviderError, cancel_stripe_subscription
from app.services.account_security_service import (consume_action_token, issue_action_token,
                                                   send_action_email)
from app.services.metrics_service import (render_metrics, request_finished, request_started,
                                          unhandled_error)
from app.services.alert_service import dispatch_operational_alert
from app.task_queue import dispatch_diagnosis_job
from sqlalchemy.exc import IntegrityError


ai_gateway = create_ai_gateway(settings.ai_provider)
web_dir = Path(__file__).resolve().parents[1] / "web"
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_runtime_settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[host.strip() for host in settings.trusted_hosts.split(",") if host.strip()],
)
app.mount("/static", StaticFiles(directory=web_dir), name="static")
if settings.partner_commerce_enabled:
    app.include_router(catalog_router)
    app.include_router(public_catalog_router)
app.include_router(billing_router)
app.include_router(telegram_router)


@app.exception_handler(HTTPException)
async def localized_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    language = normalize_language(request.headers.get("accept-language"))
    detail = translate_http_error(exc.detail, language, exc.status_code) if isinstance(exc.detail, str) else exc.detail
    return JSONResponse(status_code=exc.status_code, content={"detail": detail}, headers=exc.headers)


class RequestBodyLimitMiddleware:
    """Enforce MAX_REQUEST_BODY_MB for declared and chunked bodies alike.

    A pure ASGI middleware is used because the limit has to be applied while the
    body is streamed, and ``Content-Length`` is absent for chunked requests.
    """

    def __init__(self, app) -> None:
        self.app = app

    @staticmethod
    def _too_large_response(request: Request) -> tuple[dict, bytes]:
        language = normalize_language(request.headers.get("accept-language"))
        detail = translate_http_error("Тело запроса слишком большое", language, 413)
        payload = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
        start = {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        }
        return start, payload

    async def _reject(self, request: Request, send) -> None:
        start, payload = self._too_large_response(request)
        await send(start)
        await send({"type": "http.response.body", "body": payload, "more_body": False})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        limit = settings.max_request_body_mb * 1024 * 1024
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                too_large = int(content_length) > limit
            except ValueError:
                too_large = True
            if too_large:
                await self._reject(request, send)
                return

        consumed = 0
        overflow = False
        rejected = False

        async def limited_receive():
            nonlocal consumed, overflow
            message = await receive()
            if message.get("type") == "http.request":
                chunk = message.get("body") or b""
                consumed += len(chunk)
                if consumed > limit:
                    # Drain the stream without handing more bytes to the app.
                    overflow = True
                    return {
                        "type": "http.request",
                        "body": b"",
                        "more_body": message.get("more_body", False),
                    }
            return message

        async def limited_send(message):
            nonlocal rejected
            if not overflow:
                await send(message)
                return
            if message["type"] == "http.response.start":
                start, _ = self._too_large_response(request)
                await send(start)
                return
            if message["type"] == "http.response.body":
                if rejected:
                    return  # the 413 response is already complete
                rejected = True
                _, payload = self._too_large_response(request)
                await send(
                    {"type": "http.response.body", "body": payload, "more_body": False}
                )

        try:
            await self.app(scope, limited_receive, limited_send)
        except Exception:
            if overflow and not rejected:
                rejected = True
                await self._reject(request, send)
                return
            raise


app.add_middleware(RequestBodyLimitMiddleware)


@app.middleware("http")
async def security_headers(request, call_next):
    request_id = str(uuid4())
    request.state.request_id = request_id
    request_started()
    started_at = perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception:
        route = getattr(request.scope.get("route"), "path", "unmatched")
        unhandled_error(request.method, route)
        logger.exception(
            "unhandled API error",
            extra={"request_id": request_id, "method": request.method, "path": request.url.path},
        )
        dispatch_operational_alert(
            "api_unhandled_error",
            details={"request_id": request_id, "method": request.method, "route": route},
        )
        raise
    finally:
        route = getattr(request.scope.get("route"), "path", "unmatched")
        request_finished(request.method, route, status_code, perf_counter() - started_at)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' blob: data:; connect-src 'self'; font-src 'self'; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    )
    if settings.environment.lower() == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.get("/", response_class=FileResponse, include_in_schema=False)
def web_app() -> FileResponse:
    return FileResponse(web_dir / "index.html", media_type="text/html")


@app.get("/privacy", response_class=FileResponse, include_in_schema=False)
def privacy_page() -> FileResponse:
    return FileResponse(web_dir / "privacy.html", media_type="text/html")


@app.get("/terms", response_class=FileResponse, include_in_schema=False)
def terms_page() -> FileResponse:
    return FileResponse(web_dir / "terms.html", media_type="text/html")


@app.get("/b2b-terms", response_class=FileResponse, include_in_schema=False)
def b2b_terms_page() -> FileResponse:
    return FileResponse(web_dir / "b2b-terms.html", media_type="text/html")


@app.get("/api/v1/public/privacy-config")
def privacy_config() -> dict:
    """Publish non-secret controller and retention details used by legal pages."""
    return {
        "controller_name": settings.data_controller_name,
        "contact_email": settings.privacy_contact_email,
        "policy_version": settings.legal_document_version,
        "effective_date": settings.legal_document_effective_date,
        "retention_days": {
            "completed_jobs": settings.completed_job_retention_days,
            "notifications": settings.notification_retention_days,
            "ai_logs": settings.ai_log_retention_days,
            "integration_events": settings.integration_event_retention_days,
        },
        "unattached_photo_retention_hours": settings.unattached_photo_retention_hours,
        "processors": {
            "ai": "OpenAI" if settings.ai_provider == "openai" else None,
            "payments": "Stripe" if settings.billing_provider == "stripe" else None,
            "messaging": "Telegram" if settings.telegram_enabled else None,
            "weather": "Open-Meteo",
        },
    }


@app.get("/health")
def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "ai_provider": settings.ai_provider,
        "demo_mode": ai_gateway.demo_mode,
        "diagnosis_execution_mode": settings.diagnosis_execution_mode,
        "partner_commerce_enabled": settings.partner_commerce_enabled,
        "b2b_invoicing_enabled": settings.b2b_invoicing_enabled,
    }


@app.get("/health/live")
def liveness() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/health/ready")
def readiness() -> dict:
    checks: dict[str, str] = {}
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            from alembic.migration import MigrationContext
            from alembic.config import Config
            from alembic.script import ScriptDirectory

            current = MigrationContext.configure(connection).get_current_revision()
            head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
            if current != head:
                raise RuntimeError(f"database migration is {current!r}, expected {head!r}")
        checks["database"] = "ok"
        checks["migrations"] = "ok"
    except Exception as exc:
        logger.exception("readiness database check failed")
        dispatch_operational_alert("readiness_failed", details={"check": "database", "error_type": type(exc).__name__})
        raise HTTPException(503, {"status": "not_ready", "check": "database", "error": str(exc)[:200]}) from exc
    if settings.diagnosis_execution_mode == "celery":
        try:
            from redis import Redis
            from app.tasks import celery_app

            Redis.from_url(settings.celery_broker_url, socket_connect_timeout=1, socket_timeout=1).ping()
            checks["redis"] = "ok"
            workers = celery_app.control.inspect(timeout=1).ping() or {}
            if not workers:
                raise RuntimeError("no Celery workers responded")
            checks["worker"] = "ok"
        except Exception as exc:
            logger.exception("readiness queue check failed")
            dispatch_operational_alert("readiness_failed", details={"check": "queue", "error_type": type(exc).__name__})
            raise HTTPException(503, {"status": "not_ready", "check": "queue", "error": str(exc)[:200]}) from exc
    return {"status": "ready", "checks": checks}


@app.get("/metrics", include_in_schema=False)
def metrics(request: Request, db: Session = Depends(get_db)) -> Response:
    if not settings.metrics_enabled:
        raise HTTPException(404, "Metrics disabled")
    if settings.metrics_token:
        authorization = request.headers.get("authorization", "")
        expected = f"Bearer {settings.metrics_token}"
        if not hmac.compare_digest(authorization, expected):
            raise HTTPException(401, "Metrics authentication required")
    gauges = {
        "diagnosis_jobs_queued": db.scalar(select(func.count(DiagnosisJob.id)).where(
            DiagnosisJob.status == "queued")) or 0,
        "diagnosis_jobs_running": db.scalar(select(func.count(DiagnosisJob.id)).where(
            DiagnosisJob.status == "running")) or 0,
        "diagnosis_jobs_failed": db.scalar(select(func.count(DiagnosisJob.id)).where(
            DiagnosisJob.status == "failed")) or 0,
        "ai_requests_failed": db.scalar(select(func.count(AIRequestLog.id)).where(
            AIRequestLog.success.is_(False))) or 0,
        "notifications_pending": db.scalar(select(func.count(UserNotification.id)).where(
            UserNotification.read_at.is_(None))) or 0,
        "partner_conversions_pending": db.scalar(select(func.count(ProductLead.id)).where(
            ProductLead.status == "conversion_claimed")) or 0,
        "partner_invoices_outstanding_cents": db.scalar(select(func.coalesce(func.sum(PartnerInvoice.total_cents), 0)).where(
            PartnerInvoice.status == "issued")) or 0,
    }
    return Response(render_metrics(gauges), media_type="text/plain; version=0.0.4; charset=utf-8")


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    response.set_cookie(
        "ai_garden_refresh",
        refresh_token,
        max_age=settings.refresh_token_expire_days * 86400,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        path="/api/v1/auth",
    )


@app.post("/api/v1/auth/register", response_model=TokenRead, status_code=201)
def register(
    payload: UserRegister,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenRead:
    email = payload.email.strip().lower()
    enforce_auth_rate_limit("register", f"{request.client.host if request.client else 'unknown'}:{email}")
    if db.scalar(select(User.id).where(func.lower(User.email) == email)):
        raise HTTPException(409, "Пользователь с таким email уже зарегистрирован")
    user = User(email=email, name=payload.name, password_hash=hash_password(payload.password),
                language=payload.language, region=payload.region,
                email_verified_at=(datetime.now(timezone.utc) if settings.environment == "test" else None))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Пользователь с таким email уже зарегистрирован")
    db.refresh(user)
    pair = create_token_pair(user, db)
    _set_refresh_cookie(response, pair.refresh_token)
    if user.email_verified_at is None:
        pair.email_verification_required = True
        token = issue_action_token(db, user, "verify_email")
        try:
            send_action_email(user.email, "verify_email", token)
        except Exception:
            # The account is already committed. Keep registration idempotent and
            # let the user request a new link when the mail provider recovers.
            logger.exception("verification email delivery failed", extra={"user_id": user.id})
    return pair


@app.post("/api/v1/auth/login", response_model=TokenRead)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenRead:
    enforce_auth_rate_limit("login", f"{request.client.host if request.client else 'unknown'}:{payload.email}")
    user = db.scalar(select(User).where(func.lower(User.email) == payload.email.strip().lower()))
    if not user or user.is_blocked or not verify_password(payload.password, user.password_hash):
        raise HTTPException(401, "Неверный email или пароль")
    if user.email_verified_at is None:
        raise HTTPException(403, "Подтвердите email перед входом")
    pair = create_token_pair(user, db)
    _set_refresh_cookie(response, pair.refresh_token)
    return pair


@app.post("/api/v1/auth/refresh", response_model=TokenRead)
def refresh_session(
    request: Request,
    response: Response,
    payload: RefreshTokenRequest | None = None,
    db: Session = Depends(get_db),
) -> TokenRead:
    refresh_token = payload.refresh_token if payload else request.cookies.get("ai_garden_refresh")
    if not refresh_token:
        raise HTTPException(401, "Refresh-токен недействителен")
    pair = rotate_refresh_token(refresh_token, db)
    _set_refresh_cookie(response, pair.refresh_token)
    return pair


@app.post("/api/v1/auth/logout", status_code=204)
def logout_session(
    request: Request,
    response: Response,
    payload: RefreshTokenRequest | None = None,
    db: Session = Depends(get_db),
) -> Response:
    refresh_token = payload.refresh_token if payload else request.cookies.get("ai_garden_refresh")
    revoked = revoke_refresh_token(refresh_token, db) if refresh_token else False
    response.delete_cookie("ai_garden_refresh", path="/api/v1/auth")
    response.status_code = 204
    # 204 carries no body, so clients learn whether a session was really
    # revoked (a missing cookie would otherwise leave the access token alive).
    response.headers["X-Session-Revoked"] = "true" if revoked else "false"
    return response


@app.post("/api/v1/auth/email-verification/request")
def request_email_verification(
    payload: EmailVerificationRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    enforce_auth_rate_limit("verify_email", f"{request.client.host if request.client else 'unknown'}:{payload.email}")
    user = db.scalar(select(User).where(func.lower(User.email) == payload.email.strip().lower()))
    if user and user.email_verified_at is None and not user.is_blocked:
        token = issue_action_token(db, user, "verify_email")
        try:
            send_action_email(user.email, "verify_email", token)
        except Exception:
            logger.exception("verification email delivery failed", extra={"user_id": user.id})
    return {"accepted": True}


@app.post("/api/v1/auth/email-verification/confirm")
def confirm_email_verification(payload: ActionTokenRequest, db: Session = Depends(get_db)) -> dict:
    user = consume_action_token(db, payload.token, "verify_email")
    if not user:
        raise HTTPException(400, "Ссылка подтверждения недействительна или устарела")
    user.email_verified_at = datetime.now(timezone.utc)
    db.commit()
    return {"verified": True}


@app.post("/api/v1/auth/password-reset/request", status_code=202)
def request_password_reset(
    payload: EmailVerificationRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    enforce_auth_rate_limit("password_reset", f"{request.client.host if request.client else 'unknown'}:{payload.email}")
    user = db.scalar(select(User).where(func.lower(User.email) == payload.email.strip().lower()))
    if user and not user.is_blocked:
        token = issue_action_token(db, user, "password_reset")
        try:
            send_action_email(user.email, "password_reset", token)
        except Exception:
            logger.exception("password reset email delivery failed", extra={"user_id": user.id})
    return {"accepted": True}


@app.post("/api/v1/auth/password-reset/confirm")
def confirm_password_reset(payload: PasswordResetConfirm, db: Session = Depends(get_db)) -> dict:
    user = consume_action_token(db, payload.token, "password_reset")
    if not user:
        raise HTTPException(400, "Ссылка восстановления недействительна или устарела")
    user.password_hash = hash_password(payload.new_password)
    db.query(AuthSession).filter(AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)).update(
        {AuthSession.revoked_at: datetime.now(timezone.utc)}, synchronize_session=False)
    db.commit()
    return {"password_reset": True}


@app.get("/api/v1/users/me", response_model=UserRead)
def current_user(user: User = Depends(get_current_user)) -> User:
    return user


@app.patch("/api/v1/users/me", response_model=UserRead)
def update_current_user(
    payload: UserProfileUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    db.commit()
    db.refresh(user)
    return user


@app.get("/api/v1/users/me/export")
def export_account_data(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    gardens = list(db.scalars(select(Garden).where(Garden.user_id == user.id).order_by(Garden.id)))
    garden_ids = [item.id for item in gardens]
    plants = list(db.scalars(select(Plant).where(Plant.garden_id.in_(garden_ids)).order_by(Plant.id))) if garden_ids else []
    plant_ids = [item.id for item in plants]
    diagnoses = list(db.scalars(select(Diagnosis).where(Diagnosis.plant_id.in_(plant_ids)).order_by(Diagnosis.id))) if plant_ids else []
    care_events = list(db.scalars(select(CareEvent).where(CareEvent.plant_id.in_(plant_ids)).order_by(CareEvent.id))) if plant_ids else []
    reminders = list(db.scalars(select(Reminder).where(Reminder.plant_id.in_(plant_ids)).order_by(Reminder.id))) if plant_ids else []
    photos = list(db.scalars(select(PlantPhoto).where(PlantPhoto.plant_id.in_(plant_ids)).order_by(PlantPhoto.id))) if plant_ids else []
    diagnosis_ids = [item.id for item in diagnoses]
    revisions = list(db.scalars(select(DiagnosisRevision).where(
        DiagnosisRevision.diagnosis_id.in_(diagnosis_ids)).order_by(DiagnosisRevision.id))) if diagnosis_ids else []
    questions = list(db.scalars(select(DiagnosisQuestion).where(
        DiagnosisQuestion.diagnosis_id.in_(diagnosis_ids)).order_by(DiagnosisQuestion.id))) if diagnosis_ids else []
    question_ids = [item.id for item in questions]
    answers = list(db.scalars(select(DiagnosisAnswer).where(
        DiagnosisAnswer.question_id.in_(question_ids)).order_by(DiagnosisAnswer.id))) if question_ids else []
    feedback = list(db.scalars(select(DiagnosisFeedback).where(
        DiagnosisFeedback.user_id == user.id).order_by(DiagnosisFeedback.id)))
    notifications = list(db.scalars(select(UserNotification).where(
        UserNotification.user_id == user.id).order_by(UserNotification.id)))
    ai_requests = list(db.scalars(select(AIRequestLog).where(
        AIRequestLog.user_id == user.id).order_by(AIRequestLog.id)))
    diagnosis_jobs = list(db.scalars(select(DiagnosisJob).where(
        DiagnosisJob.user_id == user.id).order_by(DiagnosisJob.created_at)))
    auth_sessions = list(db.scalars(select(AuthSession).where(
        AuthSession.user_id == user.id).order_by(AuthSession.created_at)))
    subscription = db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    telegram_account = db.scalar(select(TelegramAccount).where(TelegramAccount.user_id == user.id))
    telegram_updates = list(db.scalars(select(TelegramUpdate).where(
        TelegramUpdate.chat_id == telegram_account.chat_id).order_by(TelegramUpdate.id)
    )) if telegram_account else []
    commercial_interactions = list(db.scalars(
        select(ProductLead).where(ProductLead.user_id == user.id).order_by(ProductLead.id)
    ))
    return {
        "exported_at": datetime.now(timezone.utc),
        "user": {"id": user.id, "email": user.email, "name": user.name, "language": user.language,
                 "region": user.region, "created_at": user.created_at},
        "gardens": [{column.name: getattr(item, column.name) for column in Garden.__table__.columns
                      if column.name != "user_id"} for item in gardens],
        "plants": [{column.name: getattr(item, column.name) for column in Plant.__table__.columns} for item in plants],
        "diagnoses": [{"id": item.id, "plant_id": item.plant_id, "symptoms": item.symptoms,
                        "damaged_part": item.damaged_part, "result": item.result,
                        "model_name": item.model_name, "prompt_version": item.prompt_version,
                        "created_at": item.created_at} for item in diagnoses],
        "diagnosis_revisions": [
            {column.name: getattr(item, column.name) for column in DiagnosisRevision.__table__.columns}
            for item in revisions
        ],
        "diagnosis_questions": [
            {column.name: getattr(item, column.name) for column in DiagnosisQuestion.__table__.columns}
            for item in questions
        ],
        "diagnosis_answers": [
            {column.name: getattr(item, column.name) for column in DiagnosisAnswer.__table__.columns}
            for item in answers
        ],
        "diagnosis_feedback": [
            {column.name: getattr(item, column.name) for column in DiagnosisFeedback.__table__.columns
             if column.name != "user_id"}
            for item in feedback
        ],
        "care_events": [{column.name: getattr(item, column.name) for column in CareEvent.__table__.columns} for item in care_events],
        "reminders": [{column.name: getattr(item, column.name) for column in Reminder.__table__.columns} for item in reminders],
        "photos": [{"id": item.id, "plant_id": item.plant_id, "content_type": item.content_type,
                    "created_at": item.created_at, "download_url": f"/api/v1/photos/{item.id}"} for item in photos],
        "commercial_interactions": [{
            "id": item.id, "product_id": item.product_id, "diagnosis_id": item.diagnosis_id,
            "source": item.source, "status": item.status, "partner_reference": item.partner_reference,
            "conversion_value_cents": item.conversion_value_cents, "claimed_at": item.claimed_at,
            "confirmed_at": item.confirmed_at, "created_at": item.created_at,
        } for item in commercial_interactions],
        "notifications": [
            {column.name: getattr(item, column.name) for column in UserNotification.__table__.columns
             if column.name != "user_id"}
            for item in notifications
        ],
        "ai_request_logs": [
            {column.name: getattr(item, column.name) for column in AIRequestLog.__table__.columns
             if column.name != "user_id"}
            for item in ai_requests
        ],
        "diagnosis_jobs": [
            {column.name: getattr(item, column.name) for column in DiagnosisJob.__table__.columns
             if column.name not in {"user_id", "execution_token", "celery_task_id"}}
            for item in diagnosis_jobs
        ],
        "auth_sessions": [{
            "id": item.id, "expires_at": item.expires_at, "revoked_at": item.revoked_at,
            "created_at": item.created_at, "last_used_at": item.last_used_at,
        } for item in auth_sessions],
        "subscription": ({
            column.name: getattr(subscription, column.name)
            for column in Subscription.__table__.columns if column.name != "user_id"
        } if subscription else None),
        "telegram_account": ({
            column.name: getattr(telegram_account, column.name)
            for column in TelegramAccount.__table__.columns if column.name != "user_id"
        } if telegram_account else None),
        "telegram_updates": [
            {column.name: getattr(item, column.name) for column in TelegramUpdate.__table__.columns}
            for item in telegram_updates
        ],
    }


@app.delete("/api/v1/users/me", status_code=202, response_model=None)
def delete_account(
    payload: AccountDeleteRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response | dict:
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(403, "Неверный пароль")
    subscription = db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    if (
        subscription
        and subscription.provider == "stripe"
        and subscription.provider_subscription_id
        and subscription.status not in {"canceled", "incomplete_expired"}
    ):
        deletion_request = stage_account_deletion(
            db, user, subscription.provider_subscription_id,
        )
        mark_cancellation_requested(db, deletion_request)
        try:
            cancel_stripe_subscription(subscription.provider_subscription_id, user.id)
        except BillingProviderError as exc:
            mark_cancellation_retry(db, deletion_request, exc)
            dispatch_operational_alert(
                "account_deletion_subscription_cancel_failed",
                details={"user_id": user.id, "attempts": deletion_request.attempts},
            )
        return {
            "status": "awaiting_subscription_cancellation",
            "account_blocked": True,
        }
    photos_to_delete = list(db.scalars(
        select(PlantPhoto).join(Plant).join(Garden).where(Garden.user_id == user.id)
    ))
    enqueue_photo_deletions(db, photos_to_delete)
    db.delete(user)
    db.commit()
    process_storage_deletion_outbox(db)
    return Response(status_code=204)


@app.patch("/api/v1/admin/users/{user_id}/blocked")
def set_user_blocked(
    user_id: int,
    blocked: bool,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict:
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "Пользователь не найден")
    if target.id == admin.id and blocked:
        raise HTTPException(409, "Администратор не может заблокировать собственную учётную запись")
    target.is_blocked = blocked
    if blocked:
        db.query(AuthSession).filter(AuthSession.user_id == target.id, AuthSession.revoked_at.is_(None)).update(
            {AuthSession.revoked_at: datetime.now(timezone.utc)}, synchronize_session=False)
    db.add(AdminAuditLog(
        admin_user_id=admin.id,
        action="user.block" if blocked else "user.unblock",
        target_type="user",
        target_id=str(target.id),
        details={"email": target.email},
    ))
    db.commit()
    return {"user_id": target.id, "blocked": target.is_blocked}


@app.get("/api/v1/admin/overview", response_model=AdminOverviewRead)
def admin_overview(
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict:
    average_rating = db.scalar(select(func.avg(DiagnosisFeedback.rating)))
    return {
        "users": db.scalar(select(func.count(User.id))) or 0,
        "gardens": db.scalar(select(func.count(Garden.id))) or 0,
        "plants": db.scalar(select(func.count(Plant.id))) or 0,
        "diagnoses": db.scalar(select(func.count(Diagnosis.id))) or 0,
        "jobs_queued": db.scalar(select(func.count(DiagnosisJob.id)).where(DiagnosisJob.status == "queued")) or 0,
        "jobs_running": db.scalar(select(func.count(DiagnosisJob.id)).where(DiagnosisJob.status == "running")) or 0,
        "jobs_failed": db.scalar(select(func.count(DiagnosisJob.id)).where(DiagnosisJob.status == "failed")) or 0,
        "ai_requests_successful": db.scalar(select(func.count(AIRequestLog.id)).where(AIRequestLog.success.is_(True))) or 0,
        "ai_requests_failed": db.scalar(select(func.count(AIRequestLog.id)).where(AIRequestLog.success.is_(False))) or 0,
        "input_tokens": db.scalar(select(func.coalesce(func.sum(AIRequestLog.input_tokens), 0))) or 0,
        "output_tokens": db.scalar(select(func.coalesce(func.sum(AIRequestLog.output_tokens), 0))) or 0,
        "estimated_cost": float(db.scalar(select(func.coalesce(func.sum(AIRequestLog.estimated_cost), 0))) or 0),
        "average_feedback_rating": float(average_rating) if average_rating is not None else None,
        "partners": db.scalar(select(func.count(Partner.id)).where(Partner.active.is_(True))) or 0,
        "active_products": db.scalar(select(func.count(Product.id)).where(Product.active.is_(True))) or 0,
        "product_leads": db.scalar(select(func.count(ProductLead.id))) or 0,
        "pending_partner_conversions": db.scalar(select(func.count(ProductLead.id)).where(
            ProductLead.status == "conversion_claimed")) or 0,
        "confirmed_partner_conversions": db.scalar(select(func.count(ProductLead.id)).where(
            ProductLead.status == "confirmed")) or 0,
        "partner_invoices_issued_cents": db.scalar(select(func.coalesce(func.sum(PartnerInvoice.total_cents), 0)).where(
            PartnerInvoice.status.in_(("issued", "paid")))) or 0,
    }


@app.get("/api/v1/admin/users", response_model=list[AdminUserSummaryRead])
def admin_users(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    garden_count = select(func.count(Garden.id)).where(Garden.user_id == User.id).correlate(User).scalar_subquery()
    plant_count = (
        select(func.count(Plant.id)).join(Garden).where(Garden.user_id == User.id).correlate(User).scalar_subquery()
    )
    diagnosis_count = (
        select(func.count(Diagnosis.id)).join(Plant).join(Garden)
        .where(Garden.user_id == User.id).correlate(User).scalar_subquery()
    )
    rows = db.execute(
        select(User, garden_count, plant_count, diagnosis_count)
        .order_by(User.created_at.desc(), User.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return [{
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "region": user.region,
        "is_admin": user.is_admin,
        "created_at": user.created_at,
        "gardens_count": gardens,
        "plants_count": plants,
        "diagnoses_count": diagnoses,
    } for user, gardens, plants, diagnoses in rows]


@app.get("/api/v1/admin/diagnosis-jobs", response_model=list[AdminJobSummaryRead])
def admin_diagnosis_jobs(
    status_filter: str | None = Query(default=None, pattern="^(queued|running|succeeded|failed)$"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    query = select(DiagnosisJob)
    if status_filter:
        query = query.where(DiagnosisJob.status == status_filter)
    jobs = list(db.scalars(query.order_by(DiagnosisJob.created_at.desc()).offset(offset).limit(limit)))
    return [{
        "id": job.id,
        "user_id": job.user_id,
        "plant_id": job.plant_id,
        "diagnosis_id": job.diagnosis_id,
        "status": job.status,
        "attempts": job.attempts,
        "error_type": job.error_type,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
    } for job in jobs]


@app.get("/api/v1/admin/ai-requests", response_model=list[AdminAIRequestRead])
def admin_ai_requests(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    logs = list(db.scalars(select(AIRequestLog).order_by(AIRequestLog.created_at.desc())
                           .offset(offset).limit(limit)))
    return [{
        "request_id": item.request_id,
        "diagnosis_id": item.diagnosis_id,
        "provider": item.provider,
        "model_name": item.model_name,
        "prompt_version": item.prompt_version,
        "success": item.success,
        "error_type": item.error_type,
        "http_status": item.http_status,
        "input_tokens": item.input_tokens,
        "output_tokens": item.output_tokens,
        "response_ms": item.response_ms,
        "estimated_cost": item.estimated_cost,
        "created_at": item.created_at,
    } for item in logs]


@app.get("/api/v1/admin/audit-logs", response_model=list[AdminAuditLogRead])
def admin_audit_logs(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[AdminAuditLog]:
    return list(db.scalars(select(AdminAuditLog).order_by(AdminAuditLog.created_at.desc())
                           .offset(offset).limit(limit)))


def _knowledge_admin_read(item: KnowledgeSourceRecord) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "url": item.url,
        "summary": item.summary,
        "keywords": item.keywords,
        "region": item.region,
        "language": item.languages,
        "plant_types": item.plant_types,
        "problem_types": item.problem_types,
        "last_verified_at": item.last_verified_at,
        "next_review_at": item.next_review_at,
        "reviewed_by": item.reviewed_by,
        "review_role": item.review_role,
        "usage_basis": item.usage_basis,
        "source_version": item.source_version,
        "active": item.active,
        "chunks": len(item.chunks),
        "embedding_model": item.chunks[0].embedding_model if item.chunks else None,
        "updated_at": item.updated_at,
    }


@app.get("/api/v1/admin/knowledge/sources", response_model=list[KnowledgeSourceAdminRead])
def admin_knowledge_sources(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    items = list(db.scalars(select(KnowledgeSourceRecord).order_by(KnowledgeSourceRecord.id)
                            .offset(offset).limit(limit)))
    return [_knowledge_admin_read(item) for item in items]


@app.post("/api/v1/admin/knowledge/sources", response_model=KnowledgeSourceAdminRead)
def admin_upsert_knowledge_source(
    payload: KnowledgeSourceUpsert,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict:
    source = KnowledgeSource.model_validate(payload.model_dump(exclude={"active"}))
    try:
        item = upsert_knowledge_source(db, source, active=payload.active)
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Источник с таким URL уже существует")
    except Exception as exc:
        db.rollback()
        logger.exception("knowledge source embedding failed")
        raise HTTPException(503, "Сервис embeddings временно недоступен") from exc
    return _knowledge_admin_read(item)


@app.post("/api/v1/admin/knowledge/sync", response_model=KnowledgeSyncRead)
def admin_sync_knowledge(
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict:
    provider = create_embedding_provider()
    try:
        changed = sync_builtin_knowledge(db, provider)
    except Exception as exc:
        db.rollback()
        logger.exception("knowledge synchronization failed")
        raise HTTPException(503, "Сервис embeddings временно недоступен") from exc
    return {
        "refreshed_sources": changed,
        "total_sources": db.scalar(select(func.count(KnowledgeSourceRecord.id))) or 0,
        "embedding_model": provider.model_name,
    }


@app.post("/api/v1/gardens", response_model=GardenRead, status_code=201)
def create_garden(payload: GardenCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Garden:
    garden = Garden(user_id=user.id, **payload.model_dump())
    db.add(garden)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Сад с таким названием уже существует")
    db.refresh(garden)
    return garden


@app.get("/api/v1/gardens", response_model=list[GardenRead])
def list_gardens(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Garden]:
    return list(db.scalars(select(Garden).where(Garden.user_id == user.id)
                           .order_by(Garden.id.desc()).offset(offset).limit(limit)))


def _owned_garden(garden_id: int, user: User, db: Session) -> Garden:
    garden = db.scalar(select(Garden).where(Garden.id == garden_id, Garden.user_id == user.id))
    if not garden:
        raise HTTPException(404, "Сад не найден")
    return garden


def _queue_photo_deletions(db: Session, photos: list[PlantPhoto]) -> None:
    enqueue_photo_deletions(db, photos)


@app.patch("/api/v1/gardens/{garden_id}", response_model=GardenRead)
def update_garden(
    garden_id: int,
    payload: GardenUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Garden:
    garden = _owned_garden(garden_id, user, db)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(garden, field, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Сад с таким названием уже существует")
    db.refresh(garden)
    return garden


@app.delete("/api/v1/gardens/{garden_id}", status_code=204)
def delete_garden(
    garden_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    garden = _owned_garden(garden_id, user, db)
    photos = list(db.scalars(
        select(PlantPhoto).join(Plant).where(Plant.garden_id == garden.id)
    ))
    _queue_photo_deletions(db, photos)
    db.delete(garden)
    db.commit()
    process_storage_deletion_outbox(db)
    return Response(status_code=204)


@app.get("/api/v1/gardens/{garden_id}/weather", response_model=WeatherForecastRead)
def garden_weather(
    garden_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> WeatherForecastRead:
    garden = _owned_garden(garden_id, user, db)
    location = garden.location or user.region
    if not location:
        raise HTTPException(422, "Укажите местоположение сада или регион профиля")
    try:
        return weather_service.get_forecast(location, user.language)
    except WeatherServiceError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/v1/plants", response_model=PlantRead, status_code=status.HTTP_201_CREATED)
def create_plant(payload: PlantCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Plant:
    garden = db.get(Garden, payload.garden_id)
    if not garden or garden.user_id != user.id:
        raise HTTPException(404, "Сад не найден")
    plant = Plant(**payload.model_dump())
    db.add(plant)
    db.commit()
    db.refresh(plant)
    return plant


@app.get("/api/v1/plants", response_model=list[PlantRead])
def list_plants(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Plant]:
    return list(db.scalars(select(Plant).join(Garden).where(Garden.user_id == user.id)
                           .order_by(Plant.id.desc()).offset(offset).limit(limit)))


@app.get("/api/v1/plants/{plant_id}", response_model=PlantRead)
def get_plant(plant_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Plant:
    plant = db.scalar(select(Plant).join(Garden).where(Plant.id == plant_id, Garden.user_id == user.id))
    if not plant:
        raise HTTPException(404, "Растение не найдено")
    return plant


@app.patch("/api/v1/plants/{plant_id}", response_model=PlantRead)
def update_plant(
    plant_id: int,
    payload: PlantUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Plant:
    plant = _owned_plant(plant_id, user, db)
    changes = payload.model_dump(exclude_unset=True)
    if "garden_id" in changes:
        _owned_garden(changes["garden_id"], user, db)
    for field, value in changes.items():
        setattr(plant, field, value)
    db.commit()
    db.refresh(plant)
    return plant


@app.delete("/api/v1/plants/{plant_id}", status_code=204)
def delete_plant(
    plant_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    plant = _owned_plant(plant_id, user, db)
    photos = list(db.scalars(select(PlantPhoto).where(PlantPhoto.plant_id == plant.id)))
    _queue_photo_deletions(db, photos)
    db.delete(plant)
    db.commit()
    process_storage_deletion_outbox(db)
    return Response(status_code=204)


def _owned_care_event(event_id: int, user: User, db: Session) -> CareEvent:
    event = db.scalar(
        select(CareEvent)
        .join(Plant)
        .join(Garden)
        .where(CareEvent.id == event_id, Garden.user_id == user.id)
    )
    if not event:
        raise HTTPException(404, "Запись ухода не найдена")
    return event


def _owned_reminder(reminder_id: int, user: User, db: Session) -> Reminder:
    reminder = db.scalar(
        select(Reminder)
        .join(Plant)
        .join(Garden)
        .where(Reminder.id == reminder_id, Garden.user_id == user.id)
    )
    if not reminder:
        raise HTTPException(404, "Напоминание не найдено")
    return reminder


def _aware_utc(value: datetime) -> datetime:
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).astimezone(timezone.utc)


def _next_reminder_occurrence(due_at: datetime, recurrence: str, timezone_name: str) -> datetime:
    zone = ZoneInfo(timezone_name)
    local = _aware_utc(due_at).astimezone(zone)
    if recurrence in {"daily", "weekly"}:
        days = 1 if recurrence == "daily" else 7
        return (local + timedelta(days=days)).astimezone(timezone.utc)
    year = local.year + (1 if local.month == 12 else 0)
    month = 1 if local.month == 12 else local.month + 1
    from calendar import monthrange
    day = min(local.day, monthrange(year, month)[1])
    return local.replace(year=year, month=month, day=day).astimezone(timezone.utc)


@app.post("/api/v1/plants/{plant_id}/care-events", response_model=CareEventRead, status_code=201)
def create_care_event(
    plant_id: int,
    payload: CareEventCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CareEvent:
    _owned_plant(plant_id, user, db)
    values = payload.model_dump()
    values["occurred_at"] = payload.occurred_at.astimezone(timezone.utc)
    event = CareEvent(plant_id=plant_id, **values)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


@app.get("/api/v1/plants/{plant_id}/care-events", response_model=list[CareEventRead])
def list_care_events(
    plant_id: int,
    start: datetime | None = None,
    end: datetime | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CareEvent]:
    _owned_plant(plant_id, user, db)
    query = select(CareEvent).where(CareEvent.plant_id == plant_id)
    if start is not None:
        query = query.where(CareEvent.occurred_at >= start)
    if end is not None:
        query = query.where(CareEvent.occurred_at <= end)
    return list(db.scalars(query.order_by(CareEvent.occurred_at.desc(), CareEvent.id.desc())
                           .offset(offset).limit(limit)))


@app.patch("/api/v1/care-events/{event_id}", response_model=CareEventRead)
def update_care_event(
    event_id: int,
    payload: CareEventUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CareEvent:
    event = _owned_care_event(event_id, user, db)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("occurred_at") is not None:
        changes["occurred_at"] = changes["occurred_at"].astimezone(timezone.utc)
    resulting_amount = changes.get("amount", event.amount)
    resulting_unit = changes.get("unit", event.unit)
    if resulting_unit and resulting_amount is None:
        raise HTTPException(422, "Единица измерения требует количества")
    for field, value in changes.items():
        setattr(event, field, value)
    db.commit()
    db.refresh(event)
    return event


@app.delete("/api/v1/care-events/{event_id}", status_code=204)
def delete_care_event(
    event_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    event = _owned_care_event(event_id, user, db)
    db.delete(event)
    db.commit()
    return Response(status_code=204)


@app.post("/api/v1/plants/{plant_id}/reminders", response_model=ReminderRead, status_code=201)
def create_reminder(
    plant_id: int,
    payload: ReminderCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Reminder:
    _owned_plant(plant_id, user, db)
    values = payload.model_dump()
    values["due_at"] = payload.due_at.astimezone(timezone.utc)
    reminder = Reminder(plant_id=plant_id, **values)
    db.add(reminder)
    db.commit()
    db.refresh(reminder)
    return reminder


@app.get("/api/v1/plants/{plant_id}/reminders", response_model=list[ReminderRead])
def list_reminders(
    plant_id: int,
    include_completed: bool = False,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Reminder]:
    _owned_plant(plant_id, user, db)
    query = select(Reminder).where(Reminder.plant_id == plant_id)
    if not include_completed:
        query = query.where(Reminder.completed_at.is_(None))
    return list(db.scalars(query.order_by(Reminder.due_at, Reminder.id).offset(offset).limit(limit)))


@app.patch("/api/v1/reminders/{reminder_id}", response_model=ReminderRead)
def update_reminder(
    reminder_id: int,
    payload: ReminderUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Reminder:
    reminder = _owned_reminder(reminder_id, user, db)
    changes = payload.model_dump(exclude_unset=True)
    completed = changes.pop("completed", None)
    snooze_minutes = changes.pop("snooze_minutes", None)
    skip_occurrence = changes.pop("skip_occurrence", None)
    if changes.get("due_at") is not None:
        changes["due_at"] = changes["due_at"].astimezone(timezone.utc)
    for field, value in changes.items():
        setattr(reminder, field, value)
    if snooze_minutes is not None:
        reminder.due_at = _aware_utc(reminder.due_at) + timedelta(minutes=snooze_minutes)
        reminder.completed_at = None
    if skip_occurrence:
        completed = True
    if completed is not None:
        was_completed = reminder.completed_at is not None
        reminder.completed_at = datetime.now(timezone.utc) if completed else None
        if completed and not was_completed and reminder.recurrence:
            db.add(Reminder(
                plant_id=reminder.plant_id,
                kind=reminder.kind,
                title=reminder.title,
                due_at=_next_reminder_occurrence(
                    reminder.due_at, reminder.recurrence, reminder.timezone,
                ),
                notes=reminder.notes,
                recurrence=reminder.recurrence,
                timezone=reminder.timezone,
                preferred_channel=reminder.preferred_channel,
            ))
    db.commit()
    db.refresh(reminder)
    return reminder


@app.delete("/api/v1/reminders/{reminder_id}", status_code=204)
def delete_reminder(
    reminder_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    reminder = _owned_reminder(reminder_id, user, db)
    db.delete(reminder)
    db.commit()
    return Response(status_code=204)


@app.get("/api/v1/calendar", response_model=list[CalendarItemRead])
def calendar_items(
    start: datetime,
    end: datetime,
    include_weather: bool = False,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    if start.tzinfo is None or start.utcoffset() is None or end.tzinfo is None or end.utcoffset() is None:
        raise HTTPException(422, "Период календаря должен включать часовой пояс")
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    if start_utc >= end_utc:
        raise HTTPException(422, "Начало периода должно быть раньше окончания")
    if end_utc - start_utc > timedelta(days=366):
        raise HTTPException(422, "Период календаря не может превышать 366 дней")

    reminders = db.execute(
        select(Reminder, Plant.name)
        .join(Plant)
        .join(Garden)
        .where(Garden.user_id == user.id, Reminder.due_at >= start_utc, Reminder.due_at < end_utc)
    ).all()
    care_events = db.execute(
        select(CareEvent, Plant.name)
        .join(Plant)
        .join(Garden)
        .where(Garden.user_id == user.id, CareEvent.occurred_at >= start_utc, CareEvent.occurred_at < end_utc)
    ).all()
    items = [
        {
            "item_type": "reminder",
            "reference_id": reminder.id,
            "plant_id": reminder.plant_id,
            "plant_name": plant_name,
            "event_type": reminder.kind,
            "title": reminder.title,
            "starts_at": reminder.due_at,
            "completed": reminder.completed,
        }
        for reminder, plant_name in reminders
    ]
    items.extend({
        "item_type": "care_event",
        "reference_id": event.id,
        "plant_id": event.plant_id,
        "plant_name": plant_name,
        "event_type": event.event_type,
        "title": event.notes or event.product or event.event_type,
        "starts_at": event.occurred_at,
        "completed": True,
    } for event, plant_name in care_events)
    gardens = list(db.scalars(select(Garden).where(Garden.user_id == user.id).order_by(Garden.id).limit(50)))
    items.extend(seasonal_calendar_items(gardens, user, start_utc, end_utc))
    if include_weather:
        gardens = gardens[:20]
        attempted = 0
        succeeded = 0
        for garden in gardens:
            location = garden.location or user.region
            if not location:
                continue
            attempted += 1
            try:
                forecast = weather_service.get_forecast(location, user.language)
            except WeatherServiceError:
                continue
            succeeded += 1
            try:
                forecast_timezone = ZoneInfo(forecast.timezone)
            except ZoneInfoNotFoundError:
                forecast_timezone = timezone.utc
            for warning in forecast.warnings:
                warning_at = datetime.combine(warning.date, datetime_time(hour=9), tzinfo=forecast_timezone)
                warning_utc = warning_at.astimezone(timezone.utc)
                if start_utc <= warning_utc < end_utc:
                    items.append({
                        "item_type": "weather_warning",
                        "reference_id": f"weather:{garden.id}:{warning.date}:{warning.kind}",
                        "garden_id": garden.id,
                        "garden_name": garden.name,
                        "event_type": warning.kind,
                        "title": warning.title,
                        "severity": warning.severity,
                        "description": warning.advice,
                        "starts_at": warning_at,
                        "completed": False,
                    })
        if attempted and not succeeded:
            raise HTTPException(503, "Погодный сервис временно недоступен")

    def sort_time(item: dict) -> datetime:
        value = item["starts_at"]
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    return sorted(items, key=sort_time)


@app.get("/api/v1/notifications", response_model=list[NotificationRead])
def list_notifications(
    unread_only: bool = False,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[UserNotification]:
    query = select(UserNotification).where(UserNotification.user_id == user.id)
    if unread_only:
        query = query.where(UserNotification.read_at.is_(None))
    return list(db.scalars(query.order_by(UserNotification.event_at.desc())
                           .offset(offset).limit(limit)))


@app.patch("/api/v1/notifications/{notification_id}/read", response_model=NotificationRead)
def mark_notification_read(
    notification_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserNotification:
    item = db.scalar(select(UserNotification).where(
        UserNotification.id == notification_id, UserNotification.user_id == user.id))
    if not item:
        raise HTTPException(404, "Уведомление не найдено")
    item.read_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(item)
    return item


@app.post("/api/v1/plants/{plant_id}/photos", response_model=list[PhotoRead], status_code=201)
async def upload_photos(plant_id: int, files: list[UploadFile] = File(...), user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[PlantPhoto]:
    enforce_rate_limit(
        "photo_upload",
        str(user.id),
        limit=settings.upload_rate_limit_attempts,
        window=settings.upload_rate_limit_window_seconds,
    )
    if not db.scalar(select(Plant.id).join(Garden).where(Plant.id == plant_id, Garden.user_id == user.id)):
        raise HTTPException(404, "Растение не найдено")
    if not 1 <= len(files) <= 5:
        raise HTTPException(400, "Загрузите от 1 до 5 фотографий")
    allowed = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    prepared: list[SanitizedImage] = []
    for upload in files:
        if upload.content_type not in allowed:
            raise HTTPException(415, "Поддерживаются только JPEG, PNG и WebP")
        content = await upload.read(settings.max_image_size_mb * 1024 * 1024 + 1)
        if len(content) > settings.max_image_size_mb * 1024 * 1024:
            raise HTTPException(413, f"Размер файла превышает {settings.max_image_size_mb} МБ")
        try:
            prepared.append(validate_and_sanitize_image(content, upload.content_type))
        except ImageValidationError as exc:
            raise HTTPException(exc.status_code, exc.detail)

    used_storage = db.scalar(
        select(func.coalesce(func.sum(PlantPhoto.size_bytes), 0))
        .join(Plant).join(Garden).where(Garden.user_id == user.id)
    ) or 0
    quota_bytes = settings.user_storage_quota_mb * 1024 * 1024
    if used_storage + sum(len(image.content) for image in prepared) > quota_bytes:
        raise HTTPException(413, "Квота хранения фотографий исчерпана")

    saved: list[PlantPhoto] = []
    stored: list[PlantPhoto] = []
    try:
        for image in prepared:
            storage_key, file_path = store_photo(
                plant_id, image.content, image.extension, image.content_type)
            photo = PlantPhoto(plant_id=plant_id, storage_key=storage_key,
                               file_path=file_path, content_type=image.content_type,
                               size_bytes=len(image.content))
            db.add(photo)
            saved.append(photo)
            stored.append(photo)
        db.commit()
    except Exception:
        db.rollback()
        for photo in stored:
            try:
                delete_photo(photo)
            except StorageError:
                logger.exception("Could not rollback stored photo")
        raise
    for photo in saved:
        db.refresh(photo)
    return saved


def _create_and_dispatch_diagnosis_job(payload: DiagnosisCreate, user: User, db: Session) -> DiagnosisJob:
    plant = db.scalar(select(Plant).join(Garden).where(Plant.id == payload.plant_id, Garden.user_id == user.id))
    if not plant:
        raise HTTPException(404, "Растение не найдено")
    photo_ids = set(payload.photo_ids)
    photos = list(db.scalars(select(PlantPhoto).where(PlantPhoto.plant_id == plant.id, PlantPhoto.id.in_(photo_ids))))
    if {photo.id for photo in photos} != photo_ids:
        raise HTTPException(400, "Некоторые фотографии не принадлежат этому растению")
    _enforce_monthly_diagnosis_quota(user, db)
    job = DiagnosisJob(
        id=str(uuid4()),
        user_id=user.id,
        plant_id=plant.id,
        status="queued",
        operation="create",
        payload=payload.model_dump(mode="json"),
    )
    db.add(job)
    db.commit()
    try:
        dispatch_diagnosis_job(job.id, gateway=ai_gateway)
    except Exception:
        logger.exception("Could not dispatch diagnosis job", extra={"job_id": job.id})
        db.refresh(job)
        job.status = "failed"
        job.error_type = "queue_unavailable"
        job.error_message = "Очередь диагностики временно недоступна"
        job.completed_at = datetime.now(timezone.utc)
        db.commit()
        raise HTTPException(503, "Очередь диагностики временно недоступна")
    db.expire_all()
    return db.get(DiagnosisJob, job.id)


@app.post("/api/v1/diagnoses", response_model=DiagnosisJobRead, status_code=202)
def create_diagnosis(payload: DiagnosisCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> DiagnosisJob:
    return _create_and_dispatch_diagnosis_job(payload, user, db)


@app.post("/api/v1/diagnoses/async", response_model=DiagnosisJobRead, status_code=202)
def create_diagnosis_job(
    payload: DiagnosisCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DiagnosisJob:
    return _create_and_dispatch_diagnosis_job(payload, user, db)


@app.get("/api/v1/diagnosis-jobs/{job_id}", response_model=DiagnosisJobRead)
def get_diagnosis_job(
    job_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DiagnosisJob:
    job = db.scalar(select(DiagnosisJob).where(DiagnosisJob.id == job_id, DiagnosisJob.user_id == user.id))
    if not job:
        raise HTTPException(404, "Задание диагностики не найдено")
    return job


def _owned_plant(plant_id: int, user: User, db: Session) -> Plant:
    plant = db.scalar(select(Plant).join(Garden).where(Plant.id == plant_id, Garden.user_id == user.id))
    if not plant:
        raise HTTPException(404, "Растение не найдено")
    return plant


def _owned_diagnosis(diagnosis_id: int, user: User, db: Session) -> Diagnosis:
    diagnosis = db.scalar(
        select(Diagnosis).join(Plant).join(Garden).where(Diagnosis.id == diagnosis_id, Garden.user_id == user.id)
    )
    if not diagnosis:
        raise HTTPException(404, "Диагностика не найдена")
    return diagnosis


def _enforce_monthly_diagnosis_quota(user: User, db: Session) -> None:
    # PostgreSQL сериализует конкурентные запросы одного пользователя; SQLite
    # игнорирует FOR UPDATE, что приемлемо только для локальной разработки.
    db.execute(select(User.id).where(User.id == user.id).with_for_update()).scalar_one()
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    used = db.scalar(
        select(func.count(DiagnosisJob.id)).where(
            DiagnosisJob.user_id == user.id,
            DiagnosisJob.created_at >= month_start,
            DiagnosisJob.operation == "create",
        )
    ) or 0
    subscription = db.scalar(select(Subscription).where(
        Subscription.user_id == user.id,
        Subscription.status.in_(("active", "trialing")),
        or_(Subscription.current_period_end.is_(None), Subscription.current_period_end > now),
    ))
    limit = settings.pro_diagnoses_per_month if subscription else settings.free_diagnoses_per_month
    if used >= limit:
        raise HTTPException(429, "Месячный лимит диагностик исчерпан")


def _enforce_reanalysis_quota(diagnosis: Diagnosis, db: Session) -> None:
    db.execute(select(Diagnosis.id).where(Diagnosis.id == diagnosis.id).with_for_update()).scalar_one()
    db.refresh(diagnosis)
    attempts = db.scalar(select(func.count(DiagnosisJob.id)).where(
        DiagnosisJob.diagnosis_id == diagnosis.id,
        DiagnosisJob.operation == "reanalyze",
    )) or 0
    if attempts >= settings.max_reanalyses_per_diagnosis:
        raise HTTPException(429, "Лимит повторных анализов для этой диагностики исчерпан")
    active_job = db.scalar(select(DiagnosisJob.id).where(
        DiagnosisJob.diagnosis_id == diagnosis.id,
        DiagnosisJob.operation == "reanalyze",
        DiagnosisJob.status.in_(("queued", "running")),
    ).limit(1))
    if active_job:
        raise HTTPException(409, "Повторный анализ этой диагностики уже выполняется")


def _queue_reanalysis_job(
    diagnosis: Diagnosis,
    user: User,
    db: Session,
    *,
    reason: str,
) -> DiagnosisJob:
    _enforce_reanalysis_quota(diagnosis, db)
    job = DiagnosisJob(
        id=str(uuid4()),
        user_id=user.id,
        plant_id=diagnosis.plant_id,
        diagnosis_id=diagnosis.id,
        operation="reanalyze",
        status="queued",
        payload={"reason": reason},
    )
    db.add(job)
    db.commit()
    try:
        dispatch_diagnosis_job(job.id, gateway=ai_gateway)
    except Exception:
        logger.exception("Could not dispatch reanalysis job", extra={"job_id": job.id})
        db.expire_all()
        failed = db.get(DiagnosisJob, job.id)
        failed.status = "failed"
        failed.error_type = "queue_unavailable"
        failed.error_message = "Очередь диагностики временно недоступна"
        failed.completed_at = datetime.now(timezone.utc)
        db.commit()
        raise HTTPException(503, "Очередь диагностики временно недоступна")
    db.expire_all()
    return db.get(DiagnosisJob, job.id)


@app.get("/api/v1/diagnoses/{diagnosis_id}", response_model=DiagnosisRead)
def get_diagnosis(diagnosis_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Diagnosis:
    return _owned_diagnosis(diagnosis_id, user, db)


@app.get("/api/v1/plants/{plant_id}/diagnoses", response_model=list[DiagnosisRead])
def list_plant_diagnoses(
    plant_id: int,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Diagnosis]:
    _owned_plant(plant_id, user, db)
    return list(db.scalars(select(Diagnosis).where(Diagnosis.plant_id == plant_id)
                           .order_by(Diagnosis.created_at.desc()).offset(offset).limit(limit)))


@app.get("/api/v1/plants/{plant_id}/history", response_model=PlantHistoryRead)
def plant_history(
    plant_id: int,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    plant = _owned_plant(plant_id, user, db)
    diagnoses = list(db.scalars(select(Diagnosis).where(Diagnosis.plant_id == plant_id)
                                .order_by(Diagnosis.created_at.desc()).offset(offset).limit(limit)))
    diagnosis_ids = [item.id for item in diagnoses]
    revisions = list(db.scalars(
        select(DiagnosisRevision)
        .where(DiagnosisRevision.diagnosis_id.in_(diagnosis_ids))
        .order_by(DiagnosisRevision.created_at.desc())
    )) if diagnosis_ids else []
    answers = list(db.execute(
        select(DiagnosisQuestion.diagnosis_id, DiagnosisQuestion.text, DiagnosisAnswer.answer, DiagnosisQuestion.revision_number)
        .join(DiagnosisAnswer, DiagnosisAnswer.question_id == DiagnosisQuestion.id)
        .where(DiagnosisQuestion.diagnosis_id.in_(diagnosis_ids))
        .order_by(DiagnosisQuestion.revision_number, DiagnosisQuestion.position)
    ).all()) if diagnosis_ids else []
    care_events = list(db.scalars(
        select(CareEvent)
        .where(CareEvent.plant_id == plant_id)
        .order_by(CareEvent.occurred_at.desc(), CareEvent.id.desc())
        .limit(200)
    ))
    reminders = list(db.scalars(
        select(Reminder)
        .where(Reminder.plant_id == plant_id)
        .order_by(Reminder.due_at, Reminder.id)
        .limit(200)
    ))
    return {
        "plant": plant,
        "diagnoses": diagnoses,
        "revisions": revisions,
        "answers": [
            {"diagnosis_id": diagnosis_id, "question": question, "answer": answer, "revision_number": revision_number}
            for diagnosis_id, question, answer, revision_number in answers
        ],
        "care_events": care_events,
        "reminders": reminders,
    }


@app.get("/api/v1/photos/{photo_id}")
def get_photo(photo_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Response:
    photo = db.scalar(
        select(PlantPhoto)
        .join(Plant)
        .join(Garden)
        .where(PlantPhoto.id == photo_id, Garden.user_id == user.id)
    )
    if not photo:
        raise HTTPException(404, "Фотография не найдена")
    try:
        target = photo_download(photo)
    except StorageError:
        raise HTTPException(404, "Файл фотографии не найден")
    if isinstance(target, str):
        return RedirectResponse(target, status_code=307, headers={"Cache-Control": "private, no-store"})
    return FileResponse(
        target,
        media_type=photo.content_type,
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


@app.delete("/api/v1/photos/{photo_id}", status_code=204)
def delete_owned_photo(
    photo_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    photo = db.scalar(
        select(PlantPhoto).join(Plant).join(Garden)
        .where(PlantPhoto.id == photo_id, Garden.user_id == user.id)
    )
    if not photo:
        raise HTTPException(404, "Фотография не найдена")
    enqueue_photo_deletions(db, [photo])
    db.delete(photo)
    db.commit()
    process_storage_deletion_outbox(db)
    return Response(status_code=204)


@app.get("/api/v1/diagnoses/{diagnosis_id}/questions", response_model=list[DiagnosisQuestionRead])
def diagnosis_questions(diagnosis_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[DiagnosisQuestion]:
    diagnosis = _owned_diagnosis(diagnosis_id, user, db)
    return list(db.scalars(
        select(DiagnosisQuestion)
        .where(DiagnosisQuestion.diagnosis_id == diagnosis.id, DiagnosisQuestion.revision_number == diagnosis.current_revision)
        .order_by(DiagnosisQuestion.position)
    ))


@app.post("/api/v1/diagnoses/{diagnosis_id}/answers", response_model=DiagnosisJobRead, status_code=202)
def answer_diagnosis_questions(diagnosis_id: int, payload: DiagnosisAnswersCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> DiagnosisJob:
    diagnosis = _owned_diagnosis(diagnosis_id, user, db)
    question_ids = [item.question_id for item in payload.answers]
    if len(question_ids) != len(set(question_ids)):
        raise HTTPException(422, "Один вопрос нельзя передать дважды")
    questions = list(db.scalars(
        select(DiagnosisQuestion).where(
            DiagnosisQuestion.id.in_(question_ids),
            DiagnosisQuestion.diagnosis_id == diagnosis.id,
            DiagnosisQuestion.revision_number == diagnosis.current_revision,
        )
    ))
    if {question.id for question in questions} != set(question_ids):
        raise HTTPException(400, "Некоторые вопросы не принадлежат текущей версии диагностики")
    by_id = {question.id: question for question in questions}
    for item in payload.answers:
        question = by_id[item.question_id]
        if question.answer:
            question.answer.answer = item.answer
        else:
            db.add(DiagnosisAnswer(question_id=question.id, answer=item.answer))
    db.flush()
    return _queue_reanalysis_job(diagnosis, user, db, reason="answers")


@app.post("/api/v1/diagnoses/{diagnosis_id}/reanalyze", response_model=DiagnosisJobRead, status_code=202)
def reanalyze_diagnosis(diagnosis_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> DiagnosisJob:
    diagnosis = _owned_diagnosis(diagnosis_id, user, db)
    return _queue_reanalysis_job(diagnosis, user, db, reason="manual")


@app.post("/api/v1/diagnoses/{diagnosis_id}/feedback", response_model=DiagnosisFeedbackRead)
def create_diagnosis_feedback(diagnosis_id: int, payload: DiagnosisFeedbackCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> DiagnosisFeedback:
    diagnosis = _owned_diagnosis(diagnosis_id, user, db)
    feedback = db.scalar(select(DiagnosisFeedback).where(
        DiagnosisFeedback.diagnosis_id == diagnosis.id,
        DiagnosisFeedback.user_id == user.id,
    ))
    if feedback:
        for field, value in payload.model_dump().items():
            setattr(feedback, field, value)
    else:
        feedback = DiagnosisFeedback(diagnosis_id=diagnosis.id, user_id=user.id, **payload.model_dump())
        db.add(feedback)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        feedback = db.scalar(select(DiagnosisFeedback).where(
            DiagnosisFeedback.diagnosis_id == diagnosis.id,
            DiagnosisFeedback.user_id == user.id,
        ))
        if not feedback:
            raise
        for field, value in payload.model_dump().items():
            setattr(feedback, field, value)
        db.commit()
    db.refresh(feedback)
    return feedback

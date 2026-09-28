from pathlib import Path
from urllib.parse import urlsplit

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "AI-помощник садовода"
    database_url: str = "sqlite:///./gardener.db"
    upload_dir: Path = Path("./uploads")
    storage_backend: str = "local"
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None
    s3_region: str = "eu-central-1"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_presigned_url_seconds: int = 300
    max_image_size_mb: int = 10
    min_image_dimension: int = 16
    max_image_dimension: int = 8_000
    max_image_pixels: int = 20_000_000
    user_storage_quota_mb: int = 250
    upload_rate_limit_attempts: int = 20
    upload_rate_limit_window_seconds: int = 3_600
    unattached_photo_retention_hours: int = 24
    ai_provider: str = "mock"
    openai_api_key: str | None = None
    openai_model: str = "gpt-5.4-mini"
    openai_timeout_seconds: float = 60
    openai_max_output_tokens: int = 2_000
    openai_input_cost_per_million: float = 0
    openai_output_cost_per_million: float = 0
    knowledge_embedding_model: str = "text-embedding-3-small"
    knowledge_embedding_dimensions: int = 256
    knowledge_search_limit: int = 4
    knowledge_query_cache_seconds: int = 900
    knowledge_query_cache_entries: int = 512
    ai_image_max_dimension: int = 2_048
    ai_image_jpeg_quality: int = 88
    ai_safety_secret: str
    partner_attribution_secret: str
    partner_click_ttl_days: int = 90
    free_diagnoses_per_month: int = 5
    max_reanalyses_per_diagnosis: int = 3
    weather_provider: str = "open_meteo"
    weather_timeout_seconds: float = 8
    weather_cache_seconds: int = 1_800
    weather_cache_entries: int = 256
    weather_forecast_days: int = 10
    weather_frost_threshold_c: float = 2
    weather_heat_threshold_c: float = 30
    weather_heavy_rain_threshold_mm: float = 20
    weather_wind_threshold_kmh: float = 50
    diagnosis_execution_mode: str = "sync"
    celery_broker_url: str = "redis://redis:6379/0"
    celery_result_backend: str = "redis://redis:6379/1"
    celery_task_timeout_seconds: int = 180
    billing_provider: str = "disabled"
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_price_pro_monthly: str | None = None
    stripe_api_base: str = "https://api.stripe.com/v1"
    stripe_api_version: str = "2026-02-25.clover"
    stripe_webhook_tolerance_seconds: int = 300
    stripe_webhook_max_bytes: int = 262_144
    billing_pro_monthly_cents: int = 900
    pro_diagnoses_per_month: int = 100
    billing_currency: str = "eur"
    billing_success_url: str = "http://localhost:8000/?billing=success"
    billing_cancel_url: str = "http://localhost:8000/?billing=cancelled"
    partner_commerce_enabled: bool = False
    b2b_invoicing_enabled: bool = False
    b2b_invoice_issuer_name: str | None = None
    b2b_invoice_issuer_registration_number: str | None = None
    b2b_invoice_issuer_vat_number: str | None = None
    b2b_invoice_issuer_address: str | None = None
    b2b_invoice_issuer_email: str | None = None
    b2b_invoice_bank_name: str | None = None
    b2b_invoice_iban: str | None = None
    b2b_invoice_swift: str | None = None
    b2b_invoice_font_path: Path | None = None
    telegram_enabled: bool = False
    telegram_bot_token: str | None = None
    telegram_webhook_secret: str | None = None
    telegram_bot_username: str | None = None
    telegram_api_base: str = "https://api.telegram.org"
    telegram_link_ttl_minutes: int = 10
    telegram_webhook_max_bytes: int = 262_144
    telegram_timezone: str = "Europe/Riga"
    environment: str = "development"
    jwt_secret: str
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 30
    auth_rate_limit_attempts: int = 10
    auth_rate_limit_window_seconds: int = 300
    completed_job_retention_days: int = 30
    diagnosis_job_max_attempts: int = 3
    diagnosis_job_recovery_grace_seconds: int = 60
    notification_retention_days: int = 90
    ai_log_retention_days: int = 180
    integration_event_retention_days: int = 180
    email_delivery_mode: str = "log"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_email: str | None = None
    public_base_url: str = "http://localhost:8000"
    trusted_hosts: str = "localhost,127.0.0.1,testserver"
    max_request_body_mb: int = 60
    data_controller_name: str | None = None
    privacy_contact_email: str | None = None
    legal_document_version: str = "2026-07-15"
    legal_document_effective_date: str = "2026-07-15"
    metrics_enabled: bool = True
    metrics_token: str | None = None
    alert_webhook_url: str | None = None
    alert_webhook_secret: str | None = None
    alert_min_interval_seconds: int = 300

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()


def validate_runtime_settings() -> None:
    if len(settings.jwt_secret) < 32:
        raise RuntimeError("JWT_SECRET должен содержать не менее 32 символов")
    if len(settings.ai_safety_secret) < 32:
        raise RuntimeError("AI_SAFETY_SECRET должен содержать не менее 32 символов")
    if settings.ai_safety_secret == settings.jwt_secret:
        raise RuntimeError("AI_SAFETY_SECRET должен отличаться от JWT_SECRET")
    if len(settings.partner_attribution_secret) < 32:
        raise RuntimeError(
            "PARTNER_ATTRIBUTION_SECRET должен содержать не менее 32 символов"
        )
    if settings.partner_attribution_secret in {
        settings.jwt_secret,
        settings.ai_safety_secret,
    }:
        raise RuntimeError("PARTNER_ATTRIBUTION_SECRET должен быть отдельным секретом")
    if not 1 <= settings.partner_click_ttl_days <= 365:
        raise RuntimeError("PARTNER_CLICK_TTL_DAYS должен быть от 1 до 365")
    if not 10 <= settings.user_storage_quota_mb <= 10_000:
        raise RuntimeError("USER_STORAGE_QUOTA_MB должен быть от 10 до 10000")
    if not 10 <= settings.max_request_body_mb <= 200:
        raise RuntimeError("MAX_REQUEST_BODY_MB должен быть от 10 до 200")
    if not 1 <= settings.upload_rate_limit_attempts <= 1_000:
        raise RuntimeError("UPLOAD_RATE_LIMIT_ATTEMPTS должен быть от 1 до 1000")
    if not 60 <= settings.upload_rate_limit_window_seconds <= 86_400:
        raise RuntimeError(
            "UPLOAD_RATE_LIMIT_WINDOW_SECONDS должен быть от 60 до 86400"
        )
    if not 1 <= settings.unattached_photo_retention_hours <= 720:
        raise RuntimeError("UNATTACHED_PHOTO_RETENTION_HOURS должен быть от 1 до 720")
    provider = settings.ai_provider.strip().lower()
    if provider not in {"mock", "openai"}:
        raise RuntimeError(f"Неподдерживаемый AI_PROVIDER={settings.ai_provider!r}")
    if not 64 <= settings.knowledge_embedding_dimensions <= 2_000:
        raise RuntimeError("KNOWLEDGE_EMBEDDING_DIMENSIONS должен быть от 64 до 2000")
    if not 1 <= settings.knowledge_search_limit <= 10:
        raise RuntimeError("KNOWLEDGE_SEARCH_LIMIT должен быть от 1 до 10")
    if not 1 <= settings.knowledge_query_cache_seconds <= 86_400:
        raise RuntimeError("KNOWLEDGE_QUERY_CACHE_SECONDS должен быть от 1 до 86400")
    if not 1 <= settings.knowledge_query_cache_entries <= 10_000:
        raise RuntimeError("KNOWLEDGE_QUERY_CACHE_ENTRIES должен быть от 1 до 10000")
    if settings.weather_provider.strip().lower() != "open_meteo":
        raise RuntimeError(
            f"Неподдерживаемый WEATHER_PROVIDER={settings.weather_provider!r}"
        )
    if not 1 <= settings.weather_forecast_days <= 16:
        raise RuntimeError("WEATHER_FORECAST_DAYS должен быть от 1 до 16")
    if not 8 <= settings.weather_cache_entries <= 5_000:
        raise RuntimeError("WEATHER_CACHE_ENTRIES должен быть от 8 до 5000")
    if settings.diagnosis_execution_mode not in {"sync", "celery"}:
        raise RuntimeError("DIAGNOSIS_EXECUTION_MODE должен быть sync или celery")
    billing_provider = settings.billing_provider.strip().lower()
    if billing_provider not in {"disabled", "stripe"}:
        raise RuntimeError("BILLING_PROVIDER должен быть disabled или stripe")
    if billing_provider == "stripe" and not all(
        (
            settings.stripe_secret_key,
            settings.stripe_webhook_secret,
            settings.stripe_price_pro_monthly,
        )
    ):
        raise RuntimeError(
            "Для Stripe нужны STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET и STRIPE_PRICE_PRO_MONTHLY"
        )
    if settings.stripe_webhook_tolerance_seconds < 30:
        raise RuntimeError("STRIPE_WEBHOOK_TOLERANCE_SECONDS должен быть не меньше 30")
    if not 1024 <= settings.stripe_webhook_max_bytes <= 1_048_576:
        raise RuntimeError("STRIPE_WEBHOOK_MAX_BYTES должен быть от 1024 до 1048576")
    if settings.telegram_enabled:
        if not all(
            (
                settings.telegram_bot_token,
                settings.telegram_webhook_secret,
                settings.telegram_bot_username,
            )
        ):
            raise RuntimeError(
                "Для Telegram нужны TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET и TELEGRAM_BOT_USERNAME"
            )
        secret = settings.telegram_webhook_secret or ""
        if not 16 <= len(secret) <= 256 or not all(
            character.isalnum() or character in "_-" for character in secret
        ):
            raise RuntimeError(
                "TELEGRAM_WEBHOOK_SECRET должен содержать 16-256 символов A-Z, a-z, 0-9, _ или -"
            )
        if not 1 <= settings.telegram_link_ttl_minutes <= 60:
            raise RuntimeError("TELEGRAM_LINK_TTL_MINUTES должен быть от 1 до 60")
        if not 1024 <= settings.telegram_webhook_max_bytes <= 1_048_576:
            raise RuntimeError(
                "TELEGRAM_WEBHOOK_MAX_BYTES должен быть от 1024 до 1048576"
            )
    if not 10 <= settings.alert_min_interval_seconds <= 86_400:
        raise RuntimeError("ALERT_MIN_INTERVAL_SECONDS должен быть от 10 до 86400")
    if settings.alert_webhook_url and urlsplit(
        settings.alert_webhook_url
    ).scheme not in {"http", "https"}:
        raise RuntimeError("ALERT_WEBHOOK_URL должен использовать http или https")
    if settings.environment.lower() == "production":
        normalized_secret = settings.jwt_secret.casefold()
        normalized_safety_secret = settings.ai_safety_secret.casefold()
        normalized_partner_secret = settings.partner_attribution_secret.casefold()
        insecure_markers = (
            "replace-with",
            "change-me",
            "development",
            "example",
            "generate-a-",
        )
        if any(marker in normalized_secret for marker in insecure_markers):
            raise RuntimeError("JWT_SECRET содержит известное шаблонное значение")
        if any(marker in normalized_safety_secret for marker in insecure_markers):
            raise RuntimeError("AI_SAFETY_SECRET содержит известное шаблонное значение")
        if any(marker in normalized_partner_secret for marker in insecure_markers):
            raise RuntimeError(
                "PARTNER_ATTRIBUTION_SECRET содержит известное шаблонное значение"
            )
        if (
            not settings.data_controller_name
            or not settings.data_controller_name.strip()
        ):
            raise RuntimeError("DATA_CONTROLLER_NAME обязателен для production")
        contact = (settings.privacy_contact_email or "").strip()
        if "@" not in contact or contact.startswith("@") or contact.endswith("@"):
            raise RuntimeError("PRIVACY_CONTACT_EMAIL обязателен для production")
        if provider == "mock":
            raise RuntimeError("Production-запуск с AI_PROVIDER=mock запрещён")
        if not settings.public_base_url.startswith("https://"):
            raise RuntimeError("Production PUBLIC_BASE_URL должен использовать HTTPS")
        if not [
            host.strip() for host in settings.trusted_hosts.split(",") if host.strip()
        ]:
            raise RuntimeError("TRUSTED_HOSTS обязателен для production")
        if provider == "openai" and not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY обязателен для production")
        if settings.diagnosis_execution_mode != "celery":
            raise RuntimeError(
                "Production-запуск требует DIAGNOSIS_EXECUTION_MODE=celery"
            )
        if settings.storage_backend != "s3" or not all(
            (
                settings.s3_bucket,
                settings.s3_access_key_id,
                settings.s3_secret_access_key,
            )
        ):
            raise RuntimeError(
                "Production-запуск требует приватное S3-совместимое хранилище"
            )
        if settings.email_delivery_mode != "smtp" or not all(
            (
                settings.smtp_host,
                settings.smtp_username,
                settings.smtp_password,
                settings.smtp_from_email,
            )
        ):
            raise RuntimeError(
                "Production-запуск требует SMTP для подтверждения email и восстановления пароля"
            )
        if settings.metrics_enabled and (
            not settings.metrics_token or len(settings.metrics_token) < 32
        ):
            raise RuntimeError(
                "Production-запуск требует METRICS_TOKEN длиной не менее 32 символов"
            )
        if settings.alert_webhook_url:
            if not settings.alert_webhook_url.startswith("https://"):
                raise RuntimeError(
                    "Production ALERT_WEBHOOK_URL должен использовать HTTPS"
                )
            if (
                not settings.alert_webhook_secret
                or len(settings.alert_webhook_secret) < 32
            ):
                raise RuntimeError(
                    "ALERT_WEBHOOK_SECRET должен содержать не менее 32 символов"
                )
        if settings.b2b_invoicing_enabled:
            invoice_required = {
                "B2B_INVOICE_ISSUER_NAME": settings.b2b_invoice_issuer_name,
                "B2B_INVOICE_ISSUER_REGISTRATION_NUMBER": settings.b2b_invoice_issuer_registration_number,
                "B2B_INVOICE_ISSUER_ADDRESS": settings.b2b_invoice_issuer_address,
                "B2B_INVOICE_ISSUER_EMAIL": settings.b2b_invoice_issuer_email,
                "B2B_INVOICE_IBAN": settings.b2b_invoice_iban,
            }
            missing_invoice_fields = [
                name
                for name, value in invoice_required.items()
                if not value or not value.strip()
            ]
            if missing_invoice_fields:
                raise RuntimeError(
                    "Для production нужны реквизиты B2B-счетов: "
                    + ", ".join(missing_invoice_fields)
                )
        if billing_provider == "stripe":
            billing_urls = (settings.billing_success_url, settings.billing_cancel_url)
            if not all(url.startswith("https://") for url in billing_urls) or any(
                "example." in url for url in billing_urls
            ):
                raise RuntimeError(
                    "Production Stripe требует явно настроенные HTTPS URL возврата"
                )
            if settings.stripe_secret_key and settings.stripe_secret_key.startswith(
                "sk_test_"
            ):
                raise RuntimeError("Production Stripe требует live secret key")

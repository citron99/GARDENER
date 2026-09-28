import base64
from io import BytesIO
from types import SimpleNamespace

from PIL import Image
from pydantic import ValidationError

from app.ai.providers.openai_multimodal import OpenAIMultimodalGateway
from app.config import settings, validate_runtime_settings
from app.models import Plant, PlantPhoto
from app.schemas import DiagnosisCreate, DiagnosisResult, PossibleCause
from app.services.ai_safety import safety_identifier_for_user
from app.services.knowledge_service import retrieve_knowledge


def valid_result() -> DiagnosisResult:
    return DiagnosisResult(
        analysis_outcome="possible_problem",
        disclaimer="Предварительная оценка, не подтверждённый диагноз.",
        analysis_status="needs_confirmation",
        possible_causes=[
            PossibleCause(
                name="Нарушение полива",
                confidence="medium",
                matched_signs=["пожелтение"],
                missing_signs=["влажность почвы"],
                checks=["проверить влажность"],
            ),
            PossibleCause(
                name="Стресс корней",
                confidence="low",
                matched_signs=["пожелтение"],
                missing_signs=["состояние корней"],
                checks=["проверить дренаж"],
            ),
        ],
        safe_actions=["наблюдать"],
        questions=["Как часто поливаете?"],
        expert_required=False,
        sources=["https://invented.invalid/source"],
    )


def test_openai_provider_sends_images_context_and_whitelists_sources(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    image_path = tmp_path / "leaf.jpg"
    original = Image.new("RGB", (3000, 1000), "green")
    original.save(image_path, format="JPEG", quality=90)
    captured = {}

    class FakeResponses:
        def parse(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                id="resp_test_123",
                output_parsed=valid_result(),
                usage=SimpleNamespace(input_tokens=120, output_tokens=80),
            )

    gateway = OpenAIMultimodalGateway.__new__(OpenAIMultimodalGateway)
    gateway.model_name = "test-vision-model"
    gateway._client = SimpleNamespace(responses=FakeResponses())
    plant = Plant(
        name="Томат",
        species="Solanum lycopersicum",
        growing_place="greenhouse",
        region="Riga",
    )
    photo = PlantPhoto(file_path=str(image_path), content_type="image/jpeg", plant_id=1)
    request = DiagnosisCreate(
        plant_id=1,
        symptoms="Старые листья заметно пожелтели после полива",
        damaged_part="leaf",
        photo_ids=[1],
    )

    analysis = gateway.analyze(
        plant,
        request,
        [photo],
        ["Поливаю каждый день"],
        safety_identifier="user_hmac_hash",
    )
    result = analysis.result

    content = captured["input"][1]["content"]
    assert any(
        item["type"] == "input_image"
        and item["image_url"].startswith("data:image/jpeg;base64,")
        for item in content
    )
    context = content[0]["text"]
    assert "Solanum lycopersicum" in context
    assert "Riga" in context
    assert "Поливаю каждый день" in context
    assert captured["store"] is False
    assert captured["safety_identifier"] == "user_hmac_hash"
    assert captured["max_output_tokens"] == 2000
    image_payload = next(
        item["image_url"] for item in content if item["type"] == "input_image"
    )
    with Image.open(
        BytesIO(base64.b64decode(image_payload.split(",", 1)[1]))
    ) as sent_image:
        assert max(sent_image.size) == 2048
        assert sent_image.format == "JPEG"
    with Image.open(image_path) as stored_image:
        assert stored_image.size == (3000, 1000)
    assert all(source.startswith("https://ipm.ucanr.edu/") for source in result.sources)
    assert "invented.invalid" not in " ".join(result.sources)
    assert analysis.usage.input_tokens == 120
    assert analysis.usage.output_tokens == 80


def test_provider_turns_model_refusal_into_safe_result(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    image_path = tmp_path / "leaf.jpg"
    Image.new("RGB", (64, 64), "green").save(image_path, format="JPEG")

    class FakeResponses:
        def parse(self, **kwargs):
            refusal = SimpleNamespace(refusal="cannot comply")
            return SimpleNamespace(
                id="resp_refusal",
                output_parsed=None,
                output=[SimpleNamespace(content=[refusal])],
                usage=None,
            )

    gateway = OpenAIMultimodalGateway.__new__(OpenAIMultimodalGateway)
    gateway.model_name = "test-vision-model"
    gateway._client = SimpleNamespace(responses=FakeResponses())
    plant = Plant(name="Томат", growing_place="greenhouse", region="Riga")
    photo = PlantPhoto(file_path=str(image_path), content_type="image/jpeg", plant_id=1)
    request = DiagnosisCreate(
        plant_id=1,
        symptoms="Невозможно понять состояние растения",
        damaged_part="leaf",
        photo_ids=[1],
    )

    result = gateway.analyze(plant, request, [photo]).result

    assert result.input_status == "insufficient_data"
    assert result.analysis_status == "cannot_analyze"
    assert result.possible_causes == []
    assert result.cannot_analyze_reason


def test_provider_rejects_fully_dark_photo_without_api_call(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", tmp_path)
    image_path = tmp_path / "dark.jpg"
    Image.new("RGB", (128, 128), "black").save(image_path, format="JPEG")

    class NoCallsAllowed:
        def parse(self, **kwargs):
            raise AssertionError("Тёмное изображение не должно отправляться провайдеру")

    gateway = OpenAIMultimodalGateway.__new__(OpenAIMultimodalGateway)
    gateway.model_name = "test-vision-model"
    gateway._client = SimpleNamespace(responses=NoCallsAllowed())
    plant = Plant(name="Томат", growing_place="greenhouse", region="Riga")
    photo = PlantPhoto(file_path=str(image_path), content_type="image/jpeg", plant_id=1)
    request = DiagnosisCreate(
        plant_id=1,
        symptoms="На фотографии растение выглядит нездоровым",
        damaged_part="leaf",
        photo_ids=[1],
    )

    analysis = gateway.analyze(plant, request, [photo])

    assert analysis.result.input_status == "poor_quality"
    assert analysis.result.image_quality == "poor"
    assert analysis.result.possible_causes == []
    assert analysis.usage.input_tokens == 0


def test_provider_error_classification_has_distinct_http_statuses():
    class RateLimitError(Exception):
        pass

    class TimeoutError(Exception):
        pass

    class BadRequestError(Exception):
        pass

    class AuthenticationError(Exception):
        pass

    class ConnectionError(Exception):
        pass

    class InternalError(Exception):
        pass

    gateway = OpenAIMultimodalGateway.__new__(OpenAIMultimodalGateway)
    gateway._openai = SimpleNamespace(
        RateLimitError=RateLimitError,
        APITimeoutError=TimeoutError,
        BadRequestError=BadRequestError,
        AuthenticationError=AuthenticationError,
        APIConnectionError=ConnectionError,
        InternalServerError=InternalError,
    )
    cases = [
        (RateLimitError(), 429, "provider_rate_limit"),
        (TimeoutError(), 504, "provider_timeout"),
        (BadRequestError(), 422, "invalid_image"),
        (AuthenticationError(), 503, "provider_auth"),
        (ConnectionError(), 503, "provider_unavailable"),
    ]
    for source, status, error_type in cases:
        classified = gateway._classify_error(source, "request_test", 123)
        assert classified.http_status == status
        assert classified.error_type == error_type
        assert classified.request_id == "request_test"


def test_diagnosis_result_allows_safe_rejection_but_not_invented_causes():
    rejected = DiagnosisResult(
        input_status="not_a_plant",
        plant_detected=False,
        image_quality="good",
        cannot_analyze_reason="На фотографии не обнаружено растение",
        analysis_outcome="cannot_analyze",
        disclaimer="Диагностика не выполнялась",
        analysis_status="cannot_analyze",
        possible_causes=[],
        safe_actions=["Загрузите фотографию растения"],
        questions=[],
        expert_required=False,
        sources=[],
    )
    assert rejected.possible_causes == []
    try:
        rejected.model_copy(
            update={"possible_causes": valid_result().possible_causes}, deep=True
        )
        DiagnosisResult.model_validate(
            rejected.model_dump() | {"possible_causes": valid_result().possible_causes}
        )
    except ValidationError:
        pass
    else:
        raise AssertionError("Для not_a_plant были приняты гипотезы")


def test_diagnosis_result_accepts_healthy_plant_without_invented_causes():
    result = DiagnosisResult(
        input_status="valid",
        plant_detected=True,
        image_quality="good",
        analysis_outcome="no_visible_problem",
        disclaimer="No visible problem was found.",
        analysis_status="preliminary",
        possible_causes=[],
        safe_actions=["Continue normal care and observation."],
        questions=[],
        expert_required=False,
        sources=[],
    )
    assert result.possible_causes == []
    assert result.analysis_outcome == "no_visible_problem"

    with __import__("pytest").raises(ValidationError):
        DiagnosisResult.model_validate(
            result.model_dump() | {"analysis_outcome": "possible_problem"}
        )


def test_knowledge_retrieval_prefers_relevant_verified_source():
    sources = retrieve_knowledge(
        "пожелтение после слишком частого полива и плохой дренаж"
    )
    assert sources
    assert any("water" in source.id for source in sources)
    assert all(
        str(source.url).startswith("https://ipm.ucanr.edu/") for source in sources
    )


def test_latvian_knowledge_retrieval_understands_watering_terms():
    sources = retrieve_knowledge("lapas kļūst dzeltenas pēc laistīšanas", region="Rīga")
    assert any(source.id == "uc-ipm-water" for source in sources)
    assert all("lv" in source.language for source in sources)


def test_knowledge_retrieval_falls_back_when_embedding_provider_fails(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("embedding rate limit")

    monkeypatch.setattr(
        "app.services.knowledge_service._retrieve_from_database", unavailable
    )
    sources = retrieve_knowledge("пожелтение после слишком частого полива")
    assert sources
    assert any("water" in source.id for source in sources)


def test_safety_identifier_is_stable_and_anonymous():
    first = safety_identifier_for_user(42)
    assert first == safety_identifier_for_user(42)
    assert first != safety_identifier_for_user(43)
    assert first.startswith("user_")
    assert first != "user_42"
    assert len(first) == len("user_") + 64


def test_production_rejects_template_jwt(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(
        settings,
        "jwt_secret",
        "replace-with-a-random-secret-at-least-32-characters-long",
    )
    try:
        validate_runtime_settings()
    except RuntimeError as exc:
        assert "шаблонное" in str(exc)
    else:
        raise AssertionError("Шаблонный JWT_SECRET был принят")


def test_production_rejects_template_safety_secret(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(
        settings, "jwt_secret", "secure-jwt-value-0123456789-ABCDEFGHIJ"
    )
    monkeypatch.setattr(
        settings,
        "ai_safety_secret",
        "replace-with-a-different-random-secret-at-least-32-characters",
    )
    try:
        validate_runtime_settings()
    except RuntimeError as exc:
        assert "AI_SAFETY_SECRET" in str(exc)
    else:
        raise AssertionError("Шаблонный AI_SAFETY_SECRET был принят")


def test_production_requires_data_controller_and_privacy_contact(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(
        settings, "jwt_secret", "secure-jwt-value-0123456789-ABCDEFGHIJ"
    )
    monkeypatch.setattr(
        settings, "ai_safety_secret", "secure-safety-value-0123456789-ABCDEFG"
    )
    monkeypatch.setattr(
        settings,
        "partner_attribution_secret",
        "secure-partner-value-0123456789-ABCDEFG",
    )
    monkeypatch.setattr(settings, "data_controller_name", None)
    monkeypatch.setattr(settings, "privacy_contact_email", None)
    try:
        validate_runtime_settings()
    except RuntimeError as exc:
        assert "DATA_CONTROLLER_NAME" in str(exc)
    else:
        raise AssertionError("Production без контролёра данных был принят")

    monkeypatch.setattr(settings, "data_controller_name", "Example SIA")
    try:
        validate_runtime_settings()
    except RuntimeError as exc:
        assert "PRIVACY_CONTACT_EMAIL" in str(exc)
    else:
        raise AssertionError("Production без privacy-контакта был принят")


def test_image_limits_are_mvp_safe():
    assert settings.max_image_pixels == 20_000_000
    assert settings.max_image_dimension == 8_000
    assert settings.ai_image_max_dimension == 2_048


def test_production_requires_background_worker(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(
        settings, "jwt_secret", "secure-jwt-value-0123456789-ABCDEFGHIJ"
    )
    monkeypatch.setattr(
        settings, "ai_safety_secret", "secure-safety-value-0123456789-ABCDEFG"
    )
    monkeypatch.setattr(settings, "data_controller_name", "Example SIA")
    monkeypatch.setattr(settings, "privacy_contact_email", "privacy@example.test")
    monkeypatch.setattr(settings, "public_base_url", "https://garden.example.test")
    monkeypatch.setattr(settings, "diagnosis_execution_mode", "sync")
    try:
        validate_runtime_settings()
    except RuntimeError as exc:
        assert "DIAGNOSIS_EXECUTION_MODE=celery" in str(exc)
    else:
        raise AssertionError("Production-запуск без Celery был принят")


def test_production_requires_protected_metrics(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(
        settings, "jwt_secret", "secure-jwt-value-0123456789-ABCDEFGHIJ"
    )
    monkeypatch.setattr(
        settings, "ai_safety_secret", "secure-safety-value-0123456789-ABCDEFG"
    )
    monkeypatch.setattr(settings, "data_controller_name", "Example SIA")
    monkeypatch.setattr(settings, "privacy_contact_email", "privacy@example.test")
    monkeypatch.setattr(settings, "public_base_url", "https://garden.example.test")
    monkeypatch.setattr(settings, "diagnosis_execution_mode", "celery")
    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(settings, "s3_bucket", "private-bucket")
    monkeypatch.setattr(settings, "s3_access_key_id", "access-key")
    monkeypatch.setattr(settings, "s3_secret_access_key", "secret-key")
    monkeypatch.setattr(settings, "email_delivery_mode", "smtp")
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.test")
    monkeypatch.setattr(settings, "smtp_username", "smtp-user")
    monkeypatch.setattr(settings, "smtp_password", "smtp-password")
    monkeypatch.setattr(settings, "smtp_from_email", "noreply@example.test")
    monkeypatch.setattr(settings, "metrics_enabled", True)
    monkeypatch.setattr(settings, "metrics_token", None)
    try:
        validate_runtime_settings()
    except RuntimeError as exc:
        assert "METRICS_TOKEN" in str(exc)
    else:
        raise AssertionError("Production-запуск с открытыми метриками был принят")


def _configure_valid_production_without_invoicing(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "live-provider-key")
    monkeypatch.setattr(
        settings, "jwt_secret", "secure-jwt-value-0123456789-ABCDEFGHIJ"
    )
    monkeypatch.setattr(
        settings, "ai_safety_secret", "secure-safety-value-0123456789-ABCDEFG"
    )
    monkeypatch.setattr(
        settings,
        "partner_attribution_secret",
        "secure-partner-value-0123456789-ABCDEFG",
    )
    monkeypatch.setattr(settings, "data_controller_name", "Garden SIA")
    monkeypatch.setattr(settings, "privacy_contact_email", "privacy@garden.test")
    monkeypatch.setattr(settings, "public_base_url", "https://garden.test")
    monkeypatch.setattr(settings, "trusted_hosts", "garden.test")
    monkeypatch.setattr(settings, "diagnosis_execution_mode", "celery")
    monkeypatch.setattr(settings, "storage_backend", "s3")
    monkeypatch.setattr(settings, "s3_bucket", "private-bucket")
    monkeypatch.setattr(settings, "s3_access_key_id", "access-key")
    monkeypatch.setattr(settings, "s3_secret_access_key", "secret-key")
    monkeypatch.setattr(settings, "email_delivery_mode", "smtp")
    monkeypatch.setattr(settings, "smtp_host", "smtp.garden.test")
    monkeypatch.setattr(settings, "smtp_username", "smtp-user")
    monkeypatch.setattr(settings, "smtp_password", "smtp-password")
    monkeypatch.setattr(settings, "smtp_from_email", "noreply@garden.test")
    monkeypatch.setattr(settings, "metrics_enabled", False)
    monkeypatch.setattr(settings, "alert_webhook_url", None)
    monkeypatch.setattr(settings, "billing_provider", "disabled")
    monkeypatch.setattr(settings, "b2b_invoicing_enabled", False)


def test_production_core_mode_does_not_require_invoice_details(monkeypatch):
    _configure_valid_production_without_invoicing(monkeypatch)
    monkeypatch.setattr(settings, "b2b_invoice_issuer_name", None)
    monkeypatch.setattr(settings, "b2b_invoice_issuer_registration_number", None)
    monkeypatch.setattr(settings, "b2b_invoice_issuer_address", None)
    monkeypatch.setattr(settings, "b2b_invoice_issuer_email", None)
    monkeypatch.setattr(settings, "b2b_invoice_iban", None)

    validate_runtime_settings()


def test_production_invoicing_mode_requires_invoice_details(monkeypatch):
    _configure_valid_production_without_invoicing(monkeypatch)
    monkeypatch.setattr(settings, "b2b_invoicing_enabled", True)
    monkeypatch.setattr(settings, "b2b_invoice_issuer_name", None)

    try:
        validate_runtime_settings()
    except RuntimeError as exc:
        assert "B2B_INVOICE_ISSUER_NAME" in str(exc)
    else:
        raise AssertionError("Production invoicing without issuer details was accepted")

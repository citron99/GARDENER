import base64
import json
import logging
import time
from collections.abc import Sequence
from io import BytesIO
from uuid import uuid4

from PIL import Image, ImageStat, UnidentifiedImageError

from app.ai.base import AIAnalysis, AIProviderError, AIUsage
from app.config import settings
from app.models import Plant, PlantPhoto
from app.schemas import DiagnosisCreate, DiagnosisResult
from app.services.knowledge_service import retrieve_knowledge
from app.services.storage_service import StorageError, read_photo

PROMPT_VERSION = "plant-diagnosis-v3"
logger = logging.getLogger(__name__)
SYSTEM_PROMPT = """Ты — осторожный AI-ассистент по диагностике растений.
Сначала определи, видно ли растение, оцени качество и достаточность входа.
Для плохого качества, отсутствия растения или недостаточных данных верни
analysis_outcome=cannot_analyze, объяснение и ноль гипотез. Только для valid входа анализируй все
фотографии вместе с описанием, контекстом растения, регионом и ответами пользователя.
Если видимых проблем нет, верни analysis_outcome=no_visible_problem и ноль гипотез.
Если проблема возможна, верни analysis_outcome=possible_problem и 1–3 разные гипотезы, явно разделяя
наблюдаемые признаки и недостающие данные. Не называй результат подтверждённым
диагнозом и не рекомендуй опасное применение пестицидов без идентификации причины.
Если данных недостаточно или возможен серьёзный риск, запроси уточнение или
рекомендуй местного агронома/лабораторию. Для плохой фотографии дай инструкции
по пересъёмке. Используй только переданные source id; source_ids означают лишь
релевантный справочный материал, а не подтверждение гипотезы. Не придумывай ссылки.
Отвечай на языке описания пользователя.
"""


class OpenAIMultimodalGateway:
    prompt_version = PROMPT_VERSION
    demo_mode = False

    def __init__(self, api_key: str, model: str, timeout_seconds: float = 60):
        try:
            import openai
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("Для AI_PROVIDER=openai установите пакет openai") from exc
        self.model_name = model
        self._openai = openai
        self._client = OpenAI(api_key=api_key, timeout=timeout_seconds)

    @staticmethod
    def _photo_content(photo: PlantPhoto, request_id: str) -> tuple[dict[str, str], bool]:
        try:
            with Image.open(BytesIO(read_photo(photo))) as image:
                image.thumbnail(
                    (settings.ai_image_max_dimension, settings.ai_image_max_dimension),
                    Image.Resampling.LANCZOS,
                )
                clean = image.convert("RGB")
                luminance = ImageStat.Stat(clean.convert("L").resize((64, 64))).mean[0]
                output = BytesIO()
                clean.save(output, format="JPEG", quality=settings.ai_image_jpeg_quality, optimize=True)
                clean.close()
            encoded = base64.b64encode(output.getvalue()).decode("ascii")
        except (UnidentifiedImageError, ValueError) as exc:
            raise AIProviderError("Файл изображения повреждён", http_status=422, error_type="invalid_image", request_id=request_id) from exc
        except (OSError, StorageError) as exc:
            raise AIProviderError("Не удалось прочитать фотографию", request_id=request_id) from exc
        return (
            {
                "type": "input_image",
                "image_url": f"data:image/jpeg;base64,{encoded}",
                "detail": "high",
            },
            luminance < 8,
        )

    @staticmethod
    def _refusal_text(response) -> str | None:
        for output in getattr(response, "output", ()):
            for item in getattr(output, "content", ()):
                refusal = getattr(item, "refusal", None)
                if refusal:
                    return str(refusal)
        return None

    @staticmethod
    def _cannot_analyze(
        reason: str,
        *,
        input_status: str = "insufficient_data",
        plant_detected: bool = True,
        image_quality: str = "acceptable",
    ) -> DiagnosisResult:
        return DiagnosisResult(
            input_status=input_status,
            plant_detected=plant_detected,
            image_quality=image_quality,
            cannot_analyze_reason=reason,
            analysis_outcome="cannot_analyze",
            disclaimer="AI не смог безопасно выполнить диагностику по этим данным.",
            analysis_status="cannot_analyze",
            possible_causes=[],
            safe_actions=["Сделайте новый чёткий снимок растения при дневном рассеянном свете."],
            questions=["Можете загрузить крупный план повреждённой части растения?"],
            expert_required=False,
            sources=[],
        )

    def _classify_error(self, exc: Exception, request_id: str, response_ms: int) -> AIProviderError:
        request_id = str(getattr(exc, "request_id", None) or request_id)
        if isinstance(exc, self._openai.RateLimitError):
            return AIProviderError("Превышен rate limit AI-провайдера", http_status=429, error_type="provider_rate_limit", request_id=request_id, response_ms=response_ms)
        if isinstance(exc, self._openai.APITimeoutError):
            return AIProviderError("Тайм-аут AI-провайдера", http_status=504, error_type="provider_timeout", request_id=request_id, response_ms=response_ms)
        if isinstance(exc, self._openai.BadRequestError):
            return AIProviderError("AI-провайдер отклонил изображение", http_status=422, error_type="invalid_image", request_id=request_id, response_ms=response_ms)
        if isinstance(exc, self._openai.AuthenticationError):
            return AIProviderError("Ошибка конфигурации AI-провайдера", http_status=503, error_type="provider_auth", request_id=request_id, response_ms=response_ms)
        if isinstance(exc, (self._openai.APIConnectionError, self._openai.InternalServerError)):
            return AIProviderError("AI-провайдер недоступен", http_status=503, error_type="provider_unavailable", request_id=request_id, response_ms=response_ms)
        return AIProviderError("Ошибка мультимодального AI-провайдера", http_status=503, error_type="provider_error", request_id=request_id, response_ms=response_ms)

    def analyze(
        self,
        plant: Plant,
        request: DiagnosisCreate,
        photos: Sequence[PlantPhoto],
        answers: Sequence[str] = (),
        safety_identifier: str | None = None,
    ) -> AIAnalysis:
        local_request_id = f"ai_{uuid4().hex}"
        started = time.perf_counter()
        if not photos:
            raise AIProviderError("Для мультимодального анализа нужна фотография", http_status=422, error_type="invalid_image", request_id=local_request_id)

        query = " ".join((request.symptoms, plant.species or "", *answers))
        knowledge = retrieve_knowledge(query, region=plant.region)
        context = {
            "plant_name": plant.name,
            "species": plant.species,
            "growing_place": plant.growing_place,
            "region": plant.region,
            "symptoms": request.symptoms,
            "damaged_part": request.damaged_part.value,
            "answers": list(answers),
            "trusted_sources": [item.model_dump(mode="json") for item in knowledge],
        }
        prepared_photos = [self._photo_content(photo, local_request_id) for photo in photos]
        if all(is_dark for _, is_dark in prepared_photos):
            response_ms = round((time.perf_counter() - started) * 1000)
            return AIAnalysis(
                result=self._cannot_analyze(
                    "Фотография слишком тёмная для надёжного анализа.",
                    input_status="poor_quality",
                    image_quality="poor",
                ),
                usage=AIUsage(request_id=local_request_id, response_ms=response_ms),
            )

        content: list[dict[str, str]] = [
            {"type": "input_text", "text": "Контекст обращения:\n" + json.dumps(context, ensure_ascii=False)},
            *(item for item, is_dark in prepared_photos if not is_dark),
        ]
        try:
            response = self._client.responses.parse(
                model=self.model_name,
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                text_format=DiagnosisResult,
                store=False,
                safety_identifier=safety_identifier,
                max_output_tokens=settings.openai_max_output_tokens,
            )
            parsed = response.output_parsed
            if parsed is None:
                refusal = self._refusal_text(response)
                if refusal:
                    parsed = self._cannot_analyze("Модель отказалась анализировать этот вход по соображениям безопасности.")
                else:
                    raise AIProviderError("AI-провайдер не вернул структурированный результат", request_id=local_request_id)
            result = DiagnosisResult.model_validate(parsed)
        except AIProviderError as exc:
            logger.warning("ai_request_failed %s", json.dumps({"request_id": exc.request_id, "error_type": exc.error_type, "response_ms": exc.response_ms}))
            raise
        except Exception as exc:
            response_ms = round((time.perf_counter() - started) * 1000)
            classified = self._classify_error(exc, local_request_id, response_ms)
            logger.error("ai_request_failed %s", json.dumps({"request_id": classified.request_id, "error_type": classified.error_type, "response_ms": response_ms}))
            raise classified from exc

        allowed_source_ids = {item.id for item in knowledge}
        source_by_id = {item.id: item for item in knowledge}
        causes = [cause.model_copy(update={"source_ids": [item for item in cause.source_ids if item in allowed_source_ids]}) for cause in result.possible_causes]
        referenced_source_ids = list(dict.fromkeys(
            source_id for cause in causes for source_id in cause.source_ids
        ))
        result = result.model_copy(update={
            "possible_causes": causes,
            "sources": [str(source_by_id[source_id].url) for source_id in referenced_source_ids],
        })
        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        response_ms = round((time.perf_counter() - started) * 1000)
        estimated_cost = (
            input_tokens * settings.openai_input_cost_per_million
            + output_tokens * settings.openai_output_cost_per_million
        ) / 1_000_000
        request_id = str(getattr(response, "id", None) or local_request_id)
        logger.info("ai_request_succeeded %s", json.dumps({"request_id": request_id, "input_tokens": input_tokens, "output_tokens": output_tokens, "response_ms": response_ms, "estimated_cost": estimated_cost}))
        return AIAnalysis(
            result=result,
            usage=AIUsage(request_id=request_id, input_tokens=input_tokens, output_tokens=output_tokens, response_ms=response_ms, estimated_cost=estimated_cost),
        )

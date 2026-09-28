import hashlib
import json
import logging
import math
import re
from collections import OrderedDict
from datetime import UTC, date, datetime, time
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Protocol

from pydantic import BaseModel, HttpUrl
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import SessionLocal
from app.models import KnowledgeChunk, KnowledgeSourceRecord

logger = logging.getLogger(__name__)


class KnowledgeSource(BaseModel):
    id: str
    title: str
    url: HttpUrl
    summary: str
    keywords: list[str]
    region: str
    language: list[str]
    plant_types: list[str]
    problem_types: list[str]
    last_verified_at: date
    next_review_at: date
    reviewed_by: str
    review_role: str
    usage_basis: str
    source_version: str


class EmbeddingProvider(Protocol):
    model_name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


_SOURCE_FILE = Path(__file__).resolve().parents[1] / "knowledge" / "sources.json"
_QUERY_CACHE: OrderedDict[tuple[str, int, str], tuple[float, list[float]]] = OrderedDict()
_QUERY_CACHE_LOCK = Lock()
_WORD_RE = re.compile(r"[\wāčēģīķļņšūžа-яё]+", re.IGNORECASE)
_CONCEPTS = {
    "water": ("полив", "вода", "влаж", "дренаж", "water", "moisture", "drain", "laist", "ūden", "mitr", "drenā"),
    "yellow": ("желт", "хлороз", "yellow", "chlorosis", "dzelt", "hloroz"),
    "pest": ("вредител", "насеком", "клещ", "pest", "insect", "mite", "kaitēk", "ērce"),
    "disease": ("болезн", "гриб", "пятн", "disease", "fung", "spot", "slimīb", "sēn", "plankum"),
    "nutrition": ("питан", "удобрен", "дефицит", "nutrient", "fertiliz", "barīb", "mēslo"),
    "roots": ("корн", "root", "sakn"),
}


def _load_sources() -> list[KnowledgeSource]:
    return [KnowledgeSource.model_validate(item) for item in json.loads(_SOURCE_FILE.read_text(encoding="utf-8"))]


def detect_language(text: str) -> str:
    lowered = text.casefold()
    if any(character in lowered for character in "āčēģīķļņšūž") or any(word in lowered.split() for word in ("lapas", "augs", "laistīšana", "kaitēkļi")):
        return "lv"
    if any("а" <= character <= "я" or character == "ё" for character in lowered):
        return "ru"
    return "en"


class LocalEmbeddingProvider:
    """Deterministic, network-free fallback for development and tests."""

    model_name = "local-hash-concepts-v1"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    @staticmethod
    def _one(text: str) -> list[float]:
        dimensions = settings.knowledge_embedding_dimensions
        vector = [0.0] * dimensions
        tokens = _WORD_RE.findall(text.casefold())
        expanded = list(tokens)
        normalized = " ".join(tokens)
        for concept, variants in _CONCEPTS.items():
            if any(variant in normalized for variant in variants):
                expanded.extend((f"concept:{concept}",) * 3)
        for token in expanded:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % dimensions
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


class OpenAIEmbeddingProvider:
    def __init__(self) -> None:
        from openai import OpenAI

        self.model_name = settings.knowledge_embedding_model
        self._client = OpenAI(api_key=settings.openai_api_key, timeout=settings.openai_timeout_seconds)

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = self._client.embeddings.create(
            model=self.model_name,
            input=texts,
            dimensions=settings.knowledge_embedding_dimensions,
            encoding_format="float",
        )
        return [list(item.embedding) for item in response.data]


def create_embedding_provider() -> EmbeddingProvider:
    if settings.ai_provider.strip().lower() == "openai" and settings.openai_api_key:
        return OpenAIEmbeddingProvider()
    return LocalEmbeddingProvider()


def clear_query_embedding_cache() -> None:
    with _QUERY_CACHE_LOCK:
        _QUERY_CACHE.clear()


def _query_embedding(provider: EmbeddingProvider, query: str) -> list[float]:
    """Cache successful query vectors without retaining symptom text as the key."""
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
    key = (provider.model_name, settings.knowledge_embedding_dimensions, digest)
    now = monotonic()
    with _QUERY_CACHE_LOCK:
        cached = _QUERY_CACHE.get(key)
        if cached and now - cached[0] <= settings.knowledge_query_cache_seconds:
            _QUERY_CACHE.move_to_end(key)
            return list(cached[1])
        if cached:
            del _QUERY_CACHE[key]
    embedding = list(provider.embed([query])[0])
    with _QUERY_CACHE_LOCK:
        _QUERY_CACHE[key] = (now, embedding)
        _QUERY_CACHE.move_to_end(key)
        while len(_QUERY_CACHE) > settings.knowledge_query_cache_entries:
            _QUERY_CACHE.popitem(last=False)
    return list(embedding)


def _embedding_text(source: KnowledgeSource) -> str:
    return "\n".join((
        source.title,
        source.summary,
        " ".join(source.keywords),
        " ".join(source.plant_types),
        " ".join(source.problem_types),
        source.region,
    ))


def sync_builtin_knowledge(db: Session, provider: EmbeddingProvider | None = None) -> int:
    """Idempotently imports reviewed bundled sources and refreshes stale vectors."""

    provider = provider or create_embedding_provider()
    changed = 0
    for source in _load_sources():
        record = db.get(KnowledgeSourceRecord, source.id)
        content = _embedding_text(source)
        needs_vector = record is None
        if record is None:
            record = KnowledgeSourceRecord(id=source.id)
            db.add(record)
        elif not record.chunks or record.chunks[0].embedding_model != provider.model_name or record.chunks[0].content != content:
            needs_vector = True
        record.title = source.title
        record.url = str(source.url)
        record.summary = source.summary
        record.keywords = source.keywords
        record.region = source.region
        record.languages = source.language
        record.plant_types = source.plant_types
        record.problem_types = source.problem_types
        record.last_verified_at = datetime.combine(source.last_verified_at, time.min, tzinfo=UTC)
        record.next_review_at = datetime.combine(source.next_review_at, time.min, tzinfo=UTC)
        record.reviewed_by = source.reviewed_by
        record.review_role = source.review_role
        record.usage_basis = source.usage_basis
        record.source_version = source.source_version
        record.active = True
        if needs_vector:
            record.chunks.clear()
            record.chunks.append(KnowledgeChunk(
                position=0,
                content=content,
                embedding_model=provider.model_name,
                embedding=provider.embed([content])[0],
            ))
            changed += 1
    db.commit()
    return changed


def upsert_knowledge_source(
    db: Session,
    source: KnowledgeSource,
    *,
    active: bool = True,
    provider: EmbeddingProvider | None = None,
) -> KnowledgeSourceRecord:
    provider = provider or create_embedding_provider()
    record = db.get(KnowledgeSourceRecord, source.id)
    content = _embedding_text(source)
    needs_vector = record is None
    if record is None:
        record = KnowledgeSourceRecord(id=source.id)
        db.add(record)
    elif not record.chunks or record.chunks[0].embedding_model != provider.model_name or record.chunks[0].content != content:
        needs_vector = True
    record.title = source.title
    record.url = str(source.url)
    record.summary = source.summary
    record.keywords = source.keywords
    record.region = source.region
    record.languages = source.language
    record.plant_types = source.plant_types
    record.problem_types = source.problem_types
    record.last_verified_at = datetime.combine(source.last_verified_at, time.min, tzinfo=UTC)
    record.next_review_at = datetime.combine(source.next_review_at, time.min, tzinfo=UTC)
    record.reviewed_by = source.reviewed_by
    record.review_role = source.review_role
    record.usage_basis = source.usage_basis
    record.source_version = source.source_version
    record.active = active
    if needs_vector:
        record.chunks.clear()
        record.chunks.append(KnowledgeChunk(
            position=0,
            content=content,
            embedding_model=provider.model_name,
            embedding=provider.embed([content])[0],
        ))
    db.commit()
    db.refresh(record)
    return record


def _as_source(record: KnowledgeSourceRecord) -> KnowledgeSource:
    verified = record.last_verified_at.date() if isinstance(record.last_verified_at, datetime) else record.last_verified_at
    return KnowledgeSource(
        id=record.id,
        title=record.title,
        url=record.url,
        summary=record.summary,
        keywords=list(record.keywords),
        region=record.region,
        language=list(record.languages),
        plant_types=list(record.plant_types),
        problem_types=list(record.problem_types),
        last_verified_at=verified,
        next_review_at=(record.next_review_at.date()
                        if isinstance(record.next_review_at, datetime) else record.next_review_at),
        reviewed_by=record.reviewed_by,
        review_role=record.review_role,
        usage_basis=record.usage_basis,
        source_version=record.source_version,
    )


def _cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=False))


def _retrieve_from_database(
    db: Session,
    query: str,
    limit: int,
    region: str | None,
    language: str,
) -> list[KnowledgeSource]:
    if not db.scalar(select(func.count()).select_from(KnowledgeSourceRecord)):
        return _retrieve_static(query, limit, region, language)
    provider = create_embedding_provider()
    query_embedding = _query_embedding(provider, query)
    filters = (
        KnowledgeSourceRecord.active.is_(True),
        KnowledgeSourceRecord.next_review_at >= datetime.now(UTC),
    )
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        # Read the distance from the same query: fetching it per chunk turned
        # one ranked SELECT into up to limit * 4 extra round trips.
        distance = KnowledgeChunk.embedding.cosine_distance(query_embedding)
        rows = db.execute(
            select(KnowledgeChunk, distance)
            .join(KnowledgeChunk.source)
            .where(*filters)
            .options(selectinload(KnowledgeChunk.source))
            .order_by(distance)
            .limit(limit * 4)
        ).all()
        chunks = [chunk for chunk, _distance in rows]
        semantic = {chunk.id: 1.0 - float(value or 1.0) for chunk, value in rows}
    else:
        chunks = list(db.scalars(
            select(KnowledgeChunk)
            .join(KnowledgeChunk.source)
            .where(*filters)
            .options(selectinload(KnowledgeChunk.source))
        ))
        semantic = {chunk.id: _cosine(query_embedding, list(chunk.embedding)) for chunk in chunks}
    normalized_region = (region or "").casefold()
    normalized_query = query.casefold()
    is_latvia = any(value in normalized_region for value in ("latvia", "latvija", "riga", "rīga", "латв"))
    keyword_hits = {
        chunk.id: sum(keyword.casefold() in normalized_query for keyword in chunk.source.keywords)
        for chunk in chunks
    }
    if any(keyword_hits.values()):
        chunks = [chunk for chunk in chunks if keyword_hits[chunk.id] > 0]
    ranked = sorted(
        chunks,
        key=lambda chunk: (
            semantic[chunk.id]
            + 0.25 * keyword_hits[chunk.id]
            + (0.08 if language in chunk.source.languages else 0)
            + (0.08 if is_latvia and chunk.source.region in {"LV", "EU"} else 0)
        ),
        reverse=True,
    )
    unique: list[KnowledgeSource] = []
    seen: set[str] = set()
    for chunk in ranked:
        if chunk.source_id not in seen:
            seen.add(chunk.source_id)
            unique.append(_as_source(chunk.source))
        if len(unique) >= limit:
            break
    return unique


def _retrieve_static(query: str, limit: int, region: str | None, language: str) -> list[KnowledgeSource]:
    normalized = query.casefold()
    sources = [source for source in _load_sources() if source.next_review_at >= date.today()]
    if not sources:
        return []
    normalized_region = (region or "").casefold()
    is_latvia = any(value in normalized_region for value in ("latvia", "latvija", "riga", "rīga", "латв"))

    def keyword_hits(item: KnowledgeSource) -> int:
        return sum(keyword.casefold() in normalized for keyword in item.keywords)

    ranked = sorted(sources, key=lambda item: keyword_hits(item) * 10 + int(language in item.language) + int(is_latvia and item.region in {"LV", "EU"}), reverse=True)
    matched = [item for item in ranked if keyword_hits(item) > 0]
    fallback = [item for item in ranked if item.id == "uc-ipm-diagnosis"]
    return (matched or fallback or ranked[:1])[:limit]


def retrieve_knowledge(query: str, limit: int | None = None, region: str | None = None, language: str | None = None) -> list[KnowledgeSource]:
    limit = limit or settings.knowledge_search_limit
    language = language or detect_language(query)
    try:
        with SessionLocal() as db:
            return _retrieve_from_database(db, query, limit, region, language)
    except (SQLAlchemyError, TimeoutError, ConnectionError, OSError) as exc:
        logger.warning("knowledge_retrieval_fallback", extra={"error_type": type(exc).__name__})
        return _retrieve_static(query, limit, region, language)
    except Exception as exc:
        # Provider SDKs expose changing timeout/rate-limit exception classes.
        # Diagnosis must degrade to reviewed static sources instead of HTTP 500.
        logger.exception("knowledge_embedding_failed", extra={"error_type": type(exc).__name__})
        return _retrieve_static(query, limit, region, language)

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import event

from app.config import settings
from app.database import SessionLocal, engine
from app.models import KnowledgeChunk, KnowledgeSourceRecord
from app.services.knowledge_service import (
    KnowledgeSource,
    _query_embedding,
    _retrieve_from_database,
    _retrieve_static,
    clear_query_embedding_cache,
)


class CountingProvider:
    model_name = "counting-v1"

    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("temporary embedding failure")
        return [[float(len(texts[0])), 1.0]]


def test_query_embedding_cache_does_not_retain_plain_query_or_cache_errors():
    clear_query_embedding_cache()
    provider = CountingProvider()
    assert _query_embedding(provider, "private symptom text") == [20.0, 1.0]
    assert _query_embedding(provider, "private symptom text") == [20.0, 1.0]
    assert provider.calls == 1

    clear_query_embedding_cache()
    failing = CountingProvider(fail=True)
    with pytest.raises(RuntimeError):
        _query_embedding(failing, "retry me")
    with pytest.raises(RuntimeError):
        _query_embedding(failing, "retry me")
    assert failing.calls == 2


def test_expired_static_knowledge_is_not_returned(monkeypatch):
    expired = KnowledgeSource(
        id="expired-source", title="Expired source", url="https://example.org/source",
        summary="An old source that must no longer be retrieved.", keywords=["water"],
        region="global", language=["en"], plant_types=["all"], problem_types=["watering"],
        last_verified_at=date(2020, 1, 1), next_review_at=date(2020, 2, 1),
        reviewed_by="test reviewer", review_role="editorial",
        usage_basis="linked_factual_summary", source_version="2020-01",
    )
    monkeypatch.setattr("app.services.knowledge_service._load_sources", lambda: [expired])
    assert _retrieve_static("water", 4, None, "en") == []


# --- PostgreSQL / pgvector ranked retrieval ----------------------------------
# The vector branch below is only reachable when the test suite runs against a
# PostgreSQL database with the pgvector extension (CI job `postgres-migrations`,
# TEST_DATABASE_URL=postgresql+psycopg://...). On SQLite it is skipped, so a
# green default run never claims coverage it does not have.


class FixedProvider:
    model_name = "fixed-test-v1"

    def __init__(self, vector: list[float]) -> None:
        self.vector = vector

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(self.vector) for _ in texts]


def _unit_vector(index: int) -> list[float]:
    vector = [0.0] * settings.knowledge_embedding_dimensions
    vector[index] = 1.0
    return vector


def _seed_sources(db, vectors: dict[str, list[float]]) -> None:
    now = datetime.now(UTC)
    for source_id, embedding in vectors.items():
        db.add(
            KnowledgeSourceRecord(
                id=source_id,
                title=f"Source {source_id}",
                url=f"https://example.org/knowledge/{source_id}",
                summary=f"Seeded source {source_id} used by the pgvector retrieval test.",
                keywords=["полив"],
                region="LV",
                languages=["ru"],
                plant_types=["all"],
                problem_types=["watering"],
                last_verified_at=now - timedelta(days=1),
                next_review_at=now + timedelta(days=30),
                reviewed_by="test reviewer",
                review_role="editorial",
                usage_basis="linked_factual_summary",
                source_version="2026-01",
                active=True,
            )
        )
        db.add(
            KnowledgeChunk(
                source_id=source_id,
                position=0,
                content=f"Полив: рекомендации из источника {source_id}.",
                embedding_model="fixed-test-v1",
                embedding=embedding,
            )
        )
    db.commit()


def _patch_provider(monkeypatch, vector: list[float]) -> None:
    clear_query_embedding_cache()
    monkeypatch.setattr(
        "app.services.knowledge_service.create_embedding_provider",
        lambda: FixedProvider(vector),
    )


def _require_postgresql() -> None:
    if engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL with the pgvector extension")


def test_postgres_vector_retrieval_ranks_by_cosine_distance(monkeypatch):
    _require_postgresql()
    with SessionLocal() as db:
        _seed_sources(db, {"closest": _unit_vector(0), "farthest": _unit_vector(1)})

        _patch_provider(monkeypatch, _unit_vector(0))
        nearest_first = [source.id for source in _retrieve_from_database(db, "полив", 5, "Latvia", "ru")]

        _patch_provider(monkeypatch, _unit_vector(1))
        farthest_first = [source.id for source in _retrieve_from_database(db, "полив", 5, "Latvia", "ru")]

    assert nearest_first == ["closest", "farthest"]
    assert farthest_first == ["farthest", "closest"]


def test_postgres_vector_retrieval_reads_distance_from_one_query(monkeypatch):
    _require_postgresql()
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with SessionLocal() as db:
        _seed_sources(db, {f"source-{index}": _unit_vector(index) for index in range(4)})
        _patch_provider(monkeypatch, _unit_vector(0))
        event.listen(engine, "before_cursor_execute", _record)
        try:
            results = _retrieve_from_database(db, "полив", 5, "Latvia", "ru")
        finally:
            event.remove(engine, "before_cursor_execute", _record)

    assert next(source.id for source in results) == "source-0"
    assert len(results) == 4
    chunk_queries = [statement for statement in statements if "knowledge_chunks" in statement]
    # A per-chunk distance lookup would emit one extra query per candidate.
    assert len(chunk_queries) == 1

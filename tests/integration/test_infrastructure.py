"""Real adapter tests, isolated by PostgreSQL schema and Redis key prefix.

No model downloads. A deterministic transcriber isolates infrastructure behavior.
"""

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo
from redis import Redis

from tests.conftest import FakeTranscriber, make_wav_bytes
from transcription_service.adapters.jobs import (
    LocalMediaStore,
    PostgresJobRepository,
    RedisJobBroker,
    new_job,
)
from transcription_service.adapters.media import PyAvMediaNormalizer
from transcription_service.core.config import Settings
from transcription_service.domain import AudioMode, ModelProfile
from transcription_service.jobs.domain import (
    JobStatus,
    PendingJobLimitReached,
    SubmissionReason,
    TranscriptionJob,
)
from transcription_service.main import create_app
from transcription_service.services.execution import InlineJobRunner
from transcription_service.services.jobs import TranscriptionJobWorker
from transcription_service.services.transcription import TranscriptionService

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.skipif(
        not os.getenv("TEST_DATABASE_URL") and not os.getenv("TEST_REDIS_URL"),
        reason="Set TEST_DATABASE_URL and TEST_REDIS_URL to run real adapter tests",
    ),
]


@pytest.fixture
def repository() -> Iterator[PostgresJobRepository]:
    database_url = os.environ["TEST_DATABASE_URL"]
    schema = f"test_{uuid4().hex}"
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        repository = PostgresJobRepository(
            make_conninfo(database_url, options=f"-csearch_path={schema}")
        )
        repository.migrate()
        repository.migrate()  # Repeated startup must preserve existing tables and indexes.
        yield repository
    finally:
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def broker() -> Iterator[RedisJobBroker]:
    url = os.environ["TEST_REDIS_URL"]
    queue = f"test:{uuid4().hex}"
    broker = RedisJobBroker(url, queue)
    try:
        yield broker
    finally:
        # Remove only this test's keys; never flush the shared Redis database.
        with Redis.from_url(url) as cleanup:
            cleanup.delete(queue, f"{queue}:worker-ready")
        broker.close()


def job() -> TranscriptionJob:
    identity = uuid4().hex
    return new_job(
        job_id=str(uuid4()),
        media_key=f"{identity}.media",
        filename="sample.wav",
        content_type="audio/wav",
        language="en",
        audio_mode=AudioMode.SPEECH,
        model_profile=ModelProfile.FASTER,
        model_revision="test-revision",
        media_sha256=identity,
        cache_key=identity,
        idempotency_key=identity,
        size_bytes=100,
        max_attempts=3,
    )


def test_api_worker_result_and_cache_with_real_adapters(
    repository: PostgresJobRepository,
    broker: RedisJobBroker,
    media_store: LocalMediaStore,
    settings: Settings,
) -> None:
    broker.heartbeat(15)
    app = create_app(
        settings, job_repository=repository, job_broker=broker, media_store=media_store
    )
    service = TranscriptionService(FakeTranscriber(), PyAvMediaNormalizer(), settings)
    worker = TranscriptionJobWorker(
        repository, broker, media_store, InlineJobRunner(service), settings
    )
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        upload = {"file": ("sample.wav", make_wav_bytes(), "audio/wav")}
        first = client.post("/v1/transcriptions", files=upload, data={"language": "en"})
        assert first.status_code == 202
        accepted = first.json()
        assert broker.dequeue(1) == accepted["id"]
        worker.process(accepted["id"])
        assert client.get(accepted["status_url"]).json()["status"] == "succeeded"
        result = client.get(accepted["result_url"])
        assert result.status_code == 200
        assert result.json()["text"] == "Hello world"
        assert "00:00:00,000 -->" in client.get(accepted["result_url"] + "?format=srt").text
        duplicate = client.post("/v1/transcriptions", files=upload, data={"language": "en"})
        assert duplicate.json()["id"] == accepted["id"]
        assert duplicate.json()["status"] == "succeeded"
        persisted = repository.get(accepted["id"])
        assert persisted is not None and persisted.result is not None
        assert persisted.result["text"] == "Hello world"


def test_atomic_claim_has_one_winner(repository: PostgresJobRepository) -> None:
    candidate = job()
    repository.create_or_reuse(candidate, max_pending_jobs=10)
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(
            pool.map(lambda _: repository.claim(candidate.id, lease_expires_at=deadline), range(4))
        )
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0].attempts == 1


def test_idempotency_and_capacity(repository: PostgresJobRepository) -> None:
    candidate = job()
    repository.create_or_reuse(candidate, max_pending_jobs=1)
    duplicate = repository.create_or_reuse(replace(candidate, id=str(uuid4())), max_pending_jobs=1)
    assert duplicate.reason is SubmissionReason.IDEMPOTENT
    assert duplicate.job.id == candidate.id
    with pytest.raises(PendingJobLimitReached):
        repository.create_or_reuse(job(), max_pending_jobs=1)


def test_stale_lease_retry_and_expiry(repository: PostgresJobRepository) -> None:
    candidate = job()
    repository.create_or_reuse(candidate, max_pending_jobs=10)
    past = datetime.now(UTC) - timedelta(seconds=10)
    assert repository.claim(candidate.id, lease_expires_at=past) is not None
    recovered = repository.recover_stale(now=datetime.now(UTC), failed_expires_at=past)
    assert recovered == [candidate.id]
    assert candidate.id in repository.list_dispatchable(now=datetime.now(UTC), limit=10)
    assert repository.claim(candidate.id, lease_expires_at=past) is not None
    assert repository.schedule_retry(
        candidate.id, code="temporary", message="Retry", available_at=past
    )
    assert repository.claim(candidate.id, lease_expires_at=past) is not None
    assert repository.recover_stale(now=datetime.now(UTC), failed_expires_at=past) == []
    failed = repository.get(candidate.id)
    assert failed is not None and failed.status is JobStatus.FAILED
    assert repository.expire_due(now=datetime.now(UTC), limit=10) == [candidate.media_key]
    expired = repository.get(candidate.id)
    assert expired is not None and expired.status is JobStatus.EXPIRED


def test_redis_fifo_and_heartbeat(broker: RedisJobBroker) -> None:
    assert not broker.worker_is_ready()
    broker.heartbeat(15)
    assert broker.worker_is_ready()
    broker.enqueue("first")
    broker.enqueue("second")
    assert broker.dequeue(1) == "first"
    assert broker.dequeue(1) == "second"
    assert broker.dequeue(1) is None


def test_successful_json_result_and_retention(repository: PostgresJobRepository) -> None:
    candidate = job()
    repository.create_or_reuse(candidate, max_pending_jobs=10)
    future = datetime.now(UTC) + timedelta(minutes=1)
    assert repository.claim(candidate.id, lease_expires_at=future) is not None
    renewed = future + timedelta(minutes=1)
    assert repository.renew_lease(candidate.id, lease_expires_at=renewed)
    running = repository.get(candidate.id)
    assert running is not None and running.lease_expires_at == renewed
    result = {"text": "שלום עולם", "segments": [{"start": 0.0, "end": 1.0, "text": "שלום"}]}
    repository.mark_succeeded(candidate.id, result, expires_at=future)
    persisted = repository.get(candidate.id)
    assert persisted is not None and persisted.result == result
    assert persisted.lease_expires_at is None
    assert repository.expire_due(now=datetime.now(UTC), limit=10) == []
    assert repository.expire_due(now=renewed, limit=10) == [candidate.media_key]
    expired = repository.get(candidate.id)
    assert expired is not None and expired.result is None

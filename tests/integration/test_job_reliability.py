"""Phase 7 reliability, idempotency, retention, and readiness tests."""

import sys
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import make_wav_bytes
from tests.job_fakes import MemoryJobBroker, MemoryJobRepository
from transcription_service.adapters.jobs import LocalMediaStore
from transcription_service.core.config import Settings
from transcription_service.jobs.domain import (
    JobExecutionResult,
    JobStatus,
    TranscriptionJob,
)
from transcription_service.main import create_app
from transcription_service.services.execution import IsolatedJobRunner
from transcription_service.services.jobs import TranscriptionJobWorker

COMPLETED_RESULT: dict[str, object] = {
    "text": "Hello world",
    "language": "en",
    "language_probability": 0.99,
    "duration_seconds": 0.25,
    "processing_time_seconds": 0.1,
    "model": "test/model",
    "segments": [{"start": 0.0, "end": 0.25, "text": "Hello world"}],
}


class SequenceRunner:
    def __init__(self, outcomes: list[JobExecutionResult]) -> None:
        self.outcomes = deque(outcomes)
        self.calls = 0

    def run(
        self,
        job: TranscriptionJob,
        media_path: Path,
        heartbeat: Callable[[], None],
    ) -> JobExecutionResult:
        del job, media_path
        heartbeat()
        self.calls += 1
        return self.outcomes.popleft()


class BlockingSuccessRunner:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self.calls = 0
        self._lock = Lock()

    def run(
        self,
        job: TranscriptionJob,
        media_path: Path,
        heartbeat: Callable[[], None],
    ) -> JobExecutionResult:
        del job, media_path, heartbeat
        with self._lock:
            self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=2)
        return JobExecutionResult.succeeded(COMPLETED_RESULT)


def _settings(settings: Settings, media_path: Path, **updates: object) -> Settings:
    return settings.model_copy(
        update={
            "async_jobs_enabled": True,
            "media_storage_path": media_path,
            "job_retry_backoff_seconds": 0,
            **updates,
        }
    )


def _app(
    settings: Settings,
    repository: MemoryJobRepository,
    broker: MemoryJobBroker,
    media_store: LocalMediaStore,
) -> FastAPI:
    return create_app(
        settings,
        job_repository=repository,
        job_broker=broker,
        media_store=media_store,
    )


def _submit(
    client: TestClient,
    *,
    media: bytes | None = None,
    language: str = "en",
    idempotency_key: str | None = None,
) -> object:
    headers = {} if idempotency_key is None else {"Idempotency-Key": idempotency_key}
    return client.post(
        "/v1/transcriptions",
        files={"file": ("sample.wav", media or make_wav_bytes(), "audio/wav")},
        data={"language": language},
        headers=headers,
    )


def test_idempotency_key_and_hash_deduplication_reuse_one_job(
    settings: Settings,
    tmp_path: Path,
) -> None:
    repository = MemoryJobRepository()
    broker = MemoryJobBroker()
    store = LocalMediaStore(tmp_path / "media")
    app_settings = _settings(settings, tmp_path / "media")

    with TestClient(_app(app_settings, repository, broker, store)) as client:
        first = _submit(client, idempotency_key="upload-123")
        second = _submit(client, idempotency_key="upload-123")
        duplicate_without_key = _submit(client)

    assert first.status_code == second.status_code == duplicate_without_key.status_code == 202
    assert first.json()["id"] == second.json()["id"] == duplicate_without_key.json()["id"]
    assert len(repository.jobs) == 1
    assert list(broker.jobs) == [first.json()["id"]]
    assert len(list((tmp_path / "media").iterdir())) == 1


def test_idempotency_key_rejects_changed_request(
    settings: Settings,
    tmp_path: Path,
) -> None:
    repository = MemoryJobRepository()
    broker = MemoryJobBroker()
    store = LocalMediaStore(tmp_path / "media")
    app_settings = _settings(settings, tmp_path / "media")

    with TestClient(_app(app_settings, repository, broker, store)) as client:
        assert _submit(client, idempotency_key="same-key").status_code == 202
        conflict = _submit(
            client,
            media=make_wav_bytes(0.5),
            idempotency_key="same-key",
        )
        invalid = _submit(client, idempotency_key="spaces are unsafe")

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_idempotency_key"
    assert len(repository.jobs) == 1


def test_key_bound_during_hash_reuse_remains_stable_after_failure(
    settings: Settings,
    tmp_path: Path,
) -> None:
    repository = MemoryJobRepository()
    broker = MemoryJobBroker()
    store = LocalMediaStore(tmp_path / "media")
    app_settings = _settings(settings, tmp_path / "media")
    worker = TranscriptionJobWorker(
        repository,
        broker,
        store,
        SequenceRunner(
            [
                JobExecutionResult.failed(
                    code="unsupported_media",
                    message="The upload is corrupt.",
                    retryable=False,
                )
            ]
        ),
        app_settings,
    )

    with TestClient(_app(app_settings, repository, broker, store)) as client:
        without_key = _submit(client).json()
        bound = _submit(client, idempotency_key="bound-later").json()
        assert bound["id"] == without_key["id"]
        worker.process(without_key["id"])
        repeated = _submit(client, idempotency_key="bound-later")

    assert repeated.status_code == 202
    assert repeated.json()["id"] == without_key["id"]
    assert repeated.json()["status"] == "failed"
    assert len(repository.jobs) == 1


def test_successful_result_is_cached_by_hash_revision_and_options(
    settings: Settings,
    tmp_path: Path,
) -> None:
    repository = MemoryJobRepository()
    broker = MemoryJobBroker()
    store = LocalMediaStore(tmp_path / "media")
    app_settings = _settings(settings, tmp_path / "media")
    runner = SequenceRunner([JobExecutionResult.succeeded(COMPLETED_RESULT)])
    worker = TranscriptionJobWorker(repository, broker, store, runner, app_settings)

    with TestClient(_app(app_settings, repository, broker, store)) as client:
        first = _submit(client).json()
        worker.process(first["id"])
        broker.jobs.clear()
        cached = _submit(client)
        different_options = _submit(client, language="he")

    assert cached.status_code == 202
    assert cached.json()["id"] == first["id"]
    assert cached.json()["status"] == "succeeded"
    assert len(broker.jobs) == 1
    assert different_options.json()["id"] != first["id"]


def test_admission_limits_pending_jobs_and_media_storage(
    settings: Settings,
    tmp_path: Path,
) -> None:
    repository = MemoryJobRepository()
    broker = MemoryJobBroker()
    store = LocalMediaStore(tmp_path / "media")
    app_settings = _settings(settings, tmp_path / "media", max_pending_jobs=1)

    with TestClient(_app(app_settings, repository, broker, store)) as client:
        assert _submit(client).status_code == 202
        full = _submit(client, media=make_wav_bytes(0.5))

    assert full.status_code == 503
    assert full.json()["error"]["code"] == "job_queue_full"

    storage_repository = MemoryJobRepository()
    storage_broker = MemoryJobBroker()
    storage_store = LocalMediaStore(tmp_path / "small-media")
    storage_settings = _settings(
        settings,
        tmp_path / "small-media",
        max_stored_media_bytes=32,
    )
    with TestClient(
        _app(storage_settings, storage_repository, storage_broker, storage_store)
    ) as client:
        storage_full = _submit(client)

    assert storage_full.status_code == 507
    assert storage_full.json()["error"]["code"] == "media_storage_full"


def test_retryable_failure_backs_off_then_succeeds(
    settings: Settings,
    tmp_path: Path,
) -> None:
    repository = MemoryJobRepository()
    broker = MemoryJobBroker()
    store = LocalMediaStore(tmp_path / "media")
    app_settings = _settings(settings, tmp_path / "media", job_max_attempts=2)
    runner = SequenceRunner(
        [
            JobExecutionResult.failed(
                code="transcription_failed",
                message="Temporary inference failure.",
                retryable=True,
            ),
            JobExecutionResult.succeeded(COMPLETED_RESULT),
        ]
    )
    worker = TranscriptionJobWorker(repository, broker, store, runner, app_settings)

    with TestClient(_app(app_settings, repository, broker, store)) as client:
        accepted = _submit(client).json()
        worker.process(accepted["id"])
        retrying = repository.get(accepted["id"])
        assert retrying is not None
        assert retrying.status is JobStatus.QUEUED
        assert retrying.attempts == 1
        worker.process(accepted["id"])
        status = client.get(accepted["status_url"]).json()

    assert runner.calls == 2
    assert status["status"] == "succeeded"
    assert status["attempts"] == 2


def test_terminal_failure_is_not_retried_and_retains_media(
    settings: Settings,
    tmp_path: Path,
) -> None:
    repository = MemoryJobRepository()
    broker = MemoryJobBroker()
    store = LocalMediaStore(tmp_path / "media")
    app_settings = _settings(settings, tmp_path / "media")
    runner = SequenceRunner(
        [
            JobExecutionResult.failed(
                code="unsupported_media",
                message="The upload is corrupt.",
                retryable=False,
            )
        ]
    )
    worker = TranscriptionJobWorker(repository, broker, store, runner, app_settings)

    with TestClient(_app(app_settings, repository, broker, store)) as client:
        accepted = _submit(client).json()
        worker.process(accepted["id"])
        response = client.get(accepted["status_url"])

    job = repository.get(accepted["id"])
    assert response.json()["status"] == "failed"
    assert response.json()["error_code"] == "unsupported_media"
    assert response.json()["attempts"] == 1
    assert runner.calls == 1
    assert job is not None
    assert store.path_for(job.media_key).exists()


def test_atomic_claim_prevents_concurrent_duplicate_attempts(
    settings: Settings,
    tmp_path: Path,
) -> None:
    repository = MemoryJobRepository()
    broker = MemoryJobBroker()
    store = LocalMediaStore(tmp_path / "media")
    app_settings = _settings(settings, tmp_path / "media")
    runner = BlockingSuccessRunner()
    worker = TranscriptionJobWorker(repository, broker, store, runner, app_settings)

    with TestClient(_app(app_settings, repository, broker, store)) as client:
        accepted = _submit(client).json()
        first = Thread(target=worker.process, args=(accepted["id"],))
        second = Thread(target=worker.process, args=(accepted["id"],))
        first.start()
        assert runner.entered.wait(timeout=2)
        second.start()
        second.join(timeout=2)
        runner.release.set()
        first.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert runner.calls == 1
    completed = repository.get(accepted["id"])
    assert completed is not None
    assert completed.status is JobStatus.SUCCEEDED
    assert completed.attempts == 1


def test_stale_claim_is_recovered_after_worker_crash(
    settings: Settings,
    tmp_path: Path,
) -> None:
    repository = MemoryJobRepository()
    broker = MemoryJobBroker()
    store = LocalMediaStore(tmp_path / "media")
    app_settings = _settings(settings, tmp_path / "media", job_max_attempts=2)
    worker = TranscriptionJobWorker(
        repository,
        broker,
        store,
        SequenceRunner([JobExecutionResult.succeeded(COMPLETED_RESULT)]),
        app_settings,
    )

    with TestClient(_app(app_settings, repository, broker, store)) as client:
        accepted = _submit(client).json()
    claimed = repository.claim(
        accepted["id"],
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    assert claimed is not None

    worker.maintain(force=True)
    recovered = repository.get(accepted["id"])
    assert recovered is not None
    assert recovered.status is JobStatus.QUEUED
    assert list(broker.jobs).count(accepted["id"]) >= 2
    worker.process(accepted["id"])
    completed = repository.get(accepted["id"])
    assert completed is not None
    assert completed.status is JobStatus.SUCCEEDED
    assert completed.attempts == 2


def test_cleanup_marks_expired_before_deleting_artifacts(
    settings: Settings,
    tmp_path: Path,
) -> None:
    repository = MemoryJobRepository()
    broker = MemoryJobBroker()
    store = LocalMediaStore(tmp_path / "media")
    app_settings = _settings(settings, tmp_path / "media")
    worker = TranscriptionJobWorker(
        repository,
        broker,
        store,
        SequenceRunner(
            [
                JobExecutionResult.failed(
                    code="unsupported_media",
                    message="Corrupt upload.",
                    retryable=False,
                )
            ]
        ),
        app_settings,
    )

    with TestClient(_app(app_settings, repository, broker, store)) as client:
        accepted = _submit(client).json()
    worker.process(accepted["id"])
    failed = repository.get(accepted["id"])
    assert failed is not None
    repository.jobs[failed.id] = replace(
        failed,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    worker.maintain(force=True)

    expired = repository.get(failed.id)
    assert expired is not None
    assert expired.status is JobStatus.EXPIRED
    assert not store.path_for(expired.media_key).exists()


def test_isolated_runner_terminates_attempt_at_hard_deadline(
    settings: Settings,
    tmp_path: Path,
) -> None:
    repository = MemoryJobRepository()
    broker = MemoryJobBroker()
    store = LocalMediaStore(tmp_path / "media")
    app_settings = _settings(settings, tmp_path / "media")
    with TestClient(_app(app_settings, repository, broker, store)) as client:
        accepted = _submit(client).json()
    job = repository.get(accepted["id"])
    assert job is not None

    def slow_command(_: TranscriptionJob, result_path: Path) -> list[str]:
        return [
            sys.executable,
            "-c",
            "import time; time.sleep(10)",
            str(result_path),
        ]

    runner = IsolatedJobRunner(0.05, command_factory=slow_command)
    started_at = monotonic()
    outcome = runner.run(job, store.path_for(job.media_key), lambda: None)

    assert monotonic() - started_at < 2
    assert outcome.error_code == "job_timeout"
    assert outcome.retryable is True


def test_readiness_checks_database_broker_and_worker_heartbeat(
    settings: Settings,
    tmp_path: Path,
) -> None:
    repository = MemoryJobRepository()
    broker = MemoryJobBroker()
    store = LocalMediaStore(tmp_path / "media")
    app_settings = _settings(settings, tmp_path / "media")

    with TestClient(_app(app_settings, repository, broker, store)) as client:
        missing_worker = client.get("/health/ready")
        broker.heartbeat(15)
        ready = client.get("/health/ready")
        repository.ping_fails = True
        database_down = client.get("/health/ready")

    assert missing_worker.status_code == 503
    assert missing_worker.json()["error"]["code"] == "job_service_not_ready"
    assert ready.status_code == 200
    assert database_down.status_code == 503
    assert database_down.json()["error"]["code"] == "job_service_not_ready"

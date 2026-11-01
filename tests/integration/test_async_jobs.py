"""Durable asynchronous job API and worker integration tests."""

from collections import deque
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import FakeTranscriber, make_wav_bytes
from transcription_service.adapters.jobs import LocalMediaStore
from transcription_service.adapters.media import PyAvMediaInspector
from transcription_service.core.config import Settings
from transcription_service.jobs.domain import JobStatus, TranscriptionJob
from transcription_service.main import create_app
from transcription_service.services.jobs import TranscriptionJobWorker
from transcription_service.services.transcription import TranscriptionService


class MemoryJobRepository:
    """Keep authoritative state across app restarts without external services."""

    def __init__(self) -> None:
        self.jobs: dict[str, TranscriptionJob] = {}

    def migrate(self) -> None:
        return None

    def ping(self) -> None:
        return None

    def create(self, job: TranscriptionJob) -> None:
        self.jobs[job.id] = job

    def get(self, job_id: str) -> TranscriptionJob | None:
        return self.jobs.get(job_id)

    def mark_running(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.status is not JobStatus.QUEUED:
            return False
        self.jobs[job_id] = replace(
            job,
            status=JobStatus.RUNNING,
            updated_at=datetime.now(UTC),
        )
        return True

    def mark_succeeded(self, job_id: str, result: dict[str, object]) -> None:
        job = self.jobs[job_id]
        self.jobs[job_id] = replace(
            job,
            status=JobStatus.SUCCEEDED,
            result=result,
            updated_at=datetime.now(UTC),
        )

    def mark_failed(self, job_id: str, *, code: str, message: str) -> None:
        job = self.jobs[job_id]
        self.jobs[job_id] = replace(
            job,
            status=JobStatus.FAILED,
            error_code=code,
            error_message=message,
            updated_at=datetime.now(UTC),
        )


class MemoryJobBroker:
    def __init__(self) -> None:
        self.jobs: deque[str] = deque()

    def ping(self) -> None:
        return None

    def enqueue(self, job_id: str) -> None:
        self.jobs.append(job_id)

    def dequeue(self, timeout_seconds: int) -> str | None:
        del timeout_seconds
        return self.jobs.popleft() if self.jobs else None

    def close(self) -> None:
        return None


def _async_settings(settings: Settings, media_path: Path) -> Settings:
    return settings.model_copy(
        update={
            "async_jobs_enabled": True,
            "media_storage_path": media_path,
        }
    )


def _async_app(
    settings: Settings,
    repository: MemoryJobRepository,
    broker: MemoryJobBroker,
    media_store: LocalMediaStore,
    *,
    transcriber_factory: Callable[[Settings], FakeTranscriber] | None = None,
) -> FastAPI:
    factory = transcriber_factory or (lambda _: FakeTranscriber())
    return create_app(
        settings,
        transcriber_factory=factory,
        job_repository=repository,
        job_broker=broker,
        media_store=media_store,
    )


def test_async_upload_is_durable_before_accepted(
    settings: Settings,
    tmp_path: Path,
) -> None:
    repository = MemoryJobRepository()
    broker = MemoryJobBroker()
    media_store = LocalMediaStore(tmp_path / "media")
    async_settings = _async_settings(settings, tmp_path / "media")

    def fail_if_loaded(_: Settings) -> FakeTranscriber:
        raise AssertionError("the API process must not load an inference model")

    with TestClient(
        _async_app(
            async_settings,
            repository,
            broker,
            media_store,
            transcriber_factory=fail_if_loaded,
        )
    ) as client:
        response = client.post(
            "/v1/transcriptions",
            files={"file": ("sample.wav", make_wav_bytes(), "audio/wav")},
            data={"language": "he"},
        )

        assert response.status_code == 202
        payload = response.json()
        job = repository.get(payload["id"])
        assert response.headers["location"] == payload["status_url"]
        assert payload["status"] == "queued"
        assert job is not None
        assert job.status is JobStatus.QUEUED
        assert media_store.path_for(job.media_key).exists()
        assert list(broker.jobs) == [job.id]

        status_response = client.get(payload["status_url"])
        assert status_response.status_code == 200
        assert status_response.json()["status"] == "queued"
        assert client.get(payload["result_url"]).status_code == 409


def test_job_survives_api_restart(settings: Settings, tmp_path: Path) -> None:
    repository = MemoryJobRepository()
    broker = MemoryJobBroker()
    media_store = LocalMediaStore(tmp_path / "media")
    async_settings = _async_settings(settings, tmp_path / "media")
    app = _async_app(async_settings, repository, broker, media_store)

    with TestClient(app) as client:
        accepted = client.post(
            "/v1/transcriptions",
            files={"file": ("sample.wav", make_wav_bytes(), "audio/wav")},
        ).json()

    restarted_app = _async_app(async_settings, repository, broker, media_store)
    with TestClient(restarted_app) as restarted_client:
        response = restarted_client.get(f"/v1/transcriptions/{accepted['id']}")

    assert response.status_code == 200
    assert response.json()["status"] == "queued"


def test_worker_completes_job_and_exposes_result(
    settings: Settings,
    tmp_path: Path,
) -> None:
    repository = MemoryJobRepository()
    broker = MemoryJobBroker()
    media_store = LocalMediaStore(tmp_path / "media")
    async_settings = _async_settings(settings, tmp_path / "media")
    app = _async_app(async_settings, repository, broker, media_store)

    with TestClient(app) as client:
        accepted = client.post(
            "/v1/transcriptions",
            files={"file": ("sample.wav", make_wav_bytes(), "audio/wav")},
            data={"language": "en"},
        ).json()

        service = TranscriptionService(
            FakeTranscriber(),
            PyAvMediaInspector(max_duration_seconds=900),
            settings,
        )
        worker = TranscriptionJobWorker(repository, broker, media_store, service)
        worker.process(accepted["id"])

        status = client.get(accepted["status_url"])
        result = client.get(accepted["result_url"])

    assert status.status_code == 200
    assert status.json()["status"] == "succeeded"
    assert result.status_code == 200
    assert result.json()["job_id"] == accepted["id"]
    assert result.json()["text"] == "Hello world"
    completed_job = repository.get(accepted["id"])
    assert completed_job is not None
    assert not media_store.path_for(completed_job.media_key).exists()


def test_async_upload_enforces_exact_file_size(
    settings: Settings,
    tmp_path: Path,
) -> None:
    repository = MemoryJobRepository()
    broker = MemoryJobBroker()
    media_store = LocalMediaStore(tmp_path / "media")
    async_settings = _async_settings(settings, tmp_path / "media").model_copy(
        update={"max_upload_bytes": 32}
    )

    with TestClient(_async_app(async_settings, repository, broker, media_store)) as client:
        response = client.post(
            "/v1/transcriptions",
            files={"file": ("sample.wav", make_wav_bytes(), "audio/wav")},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "upload_too_large"
    assert repository.jobs == {}
    assert list((tmp_path / "media").iterdir()) == []


def test_unknown_and_malformed_job_ids_return_not_found(
    settings: Settings,
    tmp_path: Path,
) -> None:
    repository = MemoryJobRepository()
    broker = MemoryJobBroker()
    media_store = LocalMediaStore(tmp_path / "media")
    app = _async_app(
        _async_settings(settings, tmp_path / "media"),
        repository,
        broker,
        media_store,
    )

    with TestClient(app) as client:
        malformed = client.get("/v1/transcriptions/not-a-uuid")
        unknown = client.get("/v1/transcriptions/00000000-0000-0000-0000-000000000000")

    assert malformed.status_code == 404
    assert malformed.json()["error"]["code"] == "job_not_found"
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "job_not_found"

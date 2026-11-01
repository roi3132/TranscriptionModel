"""Durable asynchronous job API and worker integration tests."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import FakeTranscriber, make_wav_bytes
from tests.job_fakes import MemoryJobBroker, MemoryJobRepository
from transcription_service.adapters.jobs import LocalMediaStore
from transcription_service.adapters.media import PyAvMediaNormalizer
from transcription_service.core.config import Settings
from transcription_service.jobs.domain import JobStatus
from transcription_service.main import create_app
from transcription_service.services.execution import InlineJobRunner
from transcription_service.services.jobs import TranscriptionJobWorker
from transcription_service.services.transcription import TranscriptionService


def _async_settings(settings: Settings, media_path: Path) -> Settings:
    return settings.model_copy(
        update={
            "media_storage_path": media_path,
        }
    )


def _async_app(
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


def test_async_upload_is_durable_before_accepted(
    settings: Settings,
    tmp_path: Path,
) -> None:
    repository = MemoryJobRepository()
    broker = MemoryJobBroker()
    media_store = LocalMediaStore(tmp_path / "media")
    async_settings = _async_settings(settings, tmp_path / "media")

    with TestClient(_async_app(async_settings, repository, broker, media_store)) as client:
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
            PyAvMediaNormalizer(max_duration_seconds=900),
            settings,
        )
        worker = TranscriptionJobWorker(
            repository,
            broker,
            media_store,
            InlineJobRunner(service),
            async_settings,
        )
        worker.process(accepted["id"])

        status = client.get(accepted["status_url"])
        result = client.get(accepted["result_url"])
        text_result = client.get(f"{accepted['result_url']}?format=txt")
        srt_result = client.get(f"{accepted['result_url']}?format=srt")
        vtt_result = client.get(f"{accepted['result_url']}?format=vtt")

    assert status.status_code == 200
    assert status.json()["status"] == "succeeded"
    assert result.status_code == 200
    assert result.json()["job_id"] == accepted["id"]
    assert result.json()["text"] == "Hello world"
    assert text_result.text == "Hello world\n"
    assert text_result.headers["content-disposition"].endswith('.txt"')
    assert "00:00:00,000 --> 00:00:00,250" in srt_result.text
    assert vtt_result.text.startswith("WEBVTT\n\n")
    completed_job = repository.get(accepted["id"])
    assert completed_job is not None
    assert not media_store.path_for(completed_job.media_key).exists()


def test_silent_job_succeeds_with_explicit_warning(settings: Settings, tmp_path: Path) -> None:
    repository = MemoryJobRepository()
    broker = MemoryJobBroker()
    media_store = LocalMediaStore(tmp_path / "media")
    async_settings = _async_settings(settings, tmp_path / "media")
    transcriber = FakeTranscriber()

    with TestClient(_async_app(async_settings, repository, broker, media_store)) as client:
        accepted = client.post(
            "/v1/transcriptions",
            files={"file": ("silence.wav", make_wav_bytes(sample=0), "audio/wav")},
        ).json()
        worker = TranscriptionJobWorker(
            repository,
            broker,
            media_store,
            InlineJobRunner(
                TranscriptionService(
                    transcriber,
                    PyAvMediaNormalizer(max_duration_seconds=900),
                    settings,
                )
            ),
            async_settings,
        )
        worker.process(accepted["id"])
        result = client.get(accepted["result_url"])

    assert result.status_code == 200
    assert result.json()["text"] == ""
    assert result.json()["warnings"] == ["no_speech_detected"]
    assert transcriber.requested_languages == []


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

"""Asynchronous transcription endpoint integration tests."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from tests.job_fakes import MemoryJobRepository
from transcription_service.domain import AudioMode, ModelProfile


def test_upload_returns_a_durable_job_with_normalized_options(
    client: TestClient,
    job_repository: MemoryJobRepository,
    wav_factory: Callable[[float], bytes],
) -> None:
    response = client.post(
        "/v1/transcriptions",
        files={"file": ("sample.wav", wav_factory(0.25), "audio/wav")},
        data={"language": "HE", "audio_mode": "music", "model_profile": "larger"},
    )

    assert response.status_code == 202
    payload = response.json()
    assert response.headers["location"] == payload["status_url"]
    job = job_repository.get(payload["id"])
    assert job is not None
    assert job.language == "he"
    assert job.audio_mode is AudioMode.MUSIC
    assert job.model_profile is ModelProfile.LARGER


def test_auto_language_is_stored_as_none(
    client: TestClient,
    job_repository: MemoryJobRepository,
    wav_factory: Callable[[float], bytes],
) -> None:
    response = client.post(
        "/v1/transcriptions",
        files={"file": ("sample.wav", wav_factory(0.25), "audio/wav")},
    )

    job = job_repository.get(response.json()["id"])
    assert response.status_code == 202
    assert job is not None
    assert job.language is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("language", "Hebrew"),
        ("audio_mode", "karaoke"),
        ("model_profile", "untrusted/model"),
    ],
)
def test_rejects_invalid_options(
    client: TestClient,
    wav_factory: Callable[[float], bytes],
    field: str,
    value: str,
) -> None:
    response = client.post(
        "/v1/transcriptions",
        files={"file": ("sample.wav", wav_factory(0.25), "audio/wav")},
        data={field: value},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] in {"invalid_language", "validation_error"}


def test_empty_upload_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/transcriptions",
        files={"file": ("empty.wav", b"", "audio/wav")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_upload"


def test_media_validation_is_deferred_to_the_worker(client: TestClient) -> None:
    response = client.post(
        "/v1/transcriptions",
        files={"file": ("broken.webm", b"not a webm", "video/webm")},
        data={"language": "fr"},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"


def test_upload_is_closed_after_acceptance(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    wav_factory: Callable[[float], bytes],
) -> None:
    closed_filenames: list[str | None] = []
    original_close = UploadFile.close

    async def tracked_close(upload: UploadFile) -> None:
        closed_filenames.append(upload.filename)
        await original_close(upload)

    monkeypatch.setattr(UploadFile, "close", tracked_close)
    response = client.post(
        "/v1/transcriptions",
        files={"file": ("sample.wav", wav_factory(0.25), "audio/wav")},
    )

    assert response.status_code == 202
    assert "sample.wav" in closed_filenames


def test_missing_file_uses_stable_validation_error(client: TestClient) -> None:
    response = client.post("/v1/transcriptions")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"

"""Transcription endpoint integration tests with a fake model and real PyAV."""

from collections.abc import Callable
from typing import BinaryIO

from fastapi.testclient import TestClient

from tests.conftest import FakeTranscriber
from transcription_service.core.config import Settings
from transcription_service.main import create_app
from transcription_service.services.transcription import TranscriptionResult


def test_transcribes_hebrew_audio(
    client: TestClient,
    fake_transcriber: FakeTranscriber,
    wav_factory: Callable[[float], bytes],
) -> None:
    response = client.post(
        "/v1/transcriptions",
        files={"file": ("sample.wav", wav_factory(0.25), "audio/wav")},
        data={"language": "he"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["text"] == "שלום עולם"
    assert payload["language"] == "he"
    assert payload["language_probability"] == 0.98
    assert payload["duration_seconds"] == 0.25
    assert payload["model"] == "test/fake-whisper"
    assert payload["segments"] == [{"start": 0.0, "end": 0.25, "text": "שלום עולם"}]
    assert payload["processing_time_seconds"] >= 0
    assert fake_transcriber.requested_languages == ["he"]


def test_auto_language_is_passed_to_model_as_none(
    client: TestClient,
    fake_transcriber: FakeTranscriber,
    wav_factory: Callable[[float], bytes],
) -> None:
    response = client.post(
        "/v1/transcriptions",
        files={"file": ("sample.wav", wav_factory(0.25), "application/octet-stream")},
    )

    assert response.status_code == 200
    assert fake_transcriber.requested_languages == [None]


def test_rejects_language_not_supported_by_model(
    client: TestClient,
    fake_transcriber: FakeTranscriber,
    wav_factory: Callable[[float], bytes],
) -> None:
    response = client.post(
        "/v1/transcriptions",
        files={"file": ("sample.wav", wav_factory(0.25), "audio/wav")},
        data={"language": "fr"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_language"
    assert fake_transcriber.requested_languages == []


def test_rejects_malformed_language_code(
    client: TestClient,
    wav_factory: Callable[[float], bytes],
) -> None:
    response = client.post(
        "/v1/transcriptions",
        files={"file": ("sample.wav", wav_factory(0.25), "audio/wav")},
        data={"language": "Hebrew"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_language"


def test_rejects_empty_upload(client: TestClient) -> None:
    response = client.post(
        "/v1/transcriptions",
        files={"file": ("empty.wav", b"", "audio/wav")},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "empty_upload"


def test_rejects_corrupt_audio(client: TestClient) -> None:
    response = client.post(
        "/v1/transcriptions",
        files={"file": ("broken.wav", b"not a wav", "audio/wav")},
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media"


def test_phase_one_rejects_video_content_type(
    client: TestClient,
    wav_factory: Callable[[float], bytes],
) -> None:
    response = client.post(
        "/v1/transcriptions",
        files={"file": ("sample.wav", wav_factory(0.25), "video/mp4")},
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media"


def test_rejects_upload_over_byte_limit(
    settings: Settings,
    fake_transcriber: FakeTranscriber,
    wav_factory: Callable[[float], bytes],
) -> None:
    limited_settings = settings.model_copy(update={"max_upload_bytes": 32})
    limited_app = create_app(limited_settings, transcriber_factory=lambda _: fake_transcriber)

    with TestClient(limited_app) as limited_client:
        response = limited_client.post(
            "/v1/transcriptions",
            files={"file": ("sample.wav", wav_factory(0.25), "audio/wav")},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "upload_too_large"
    assert fake_transcriber.requested_languages == []


def test_rejects_audio_over_duration_limit_before_inference(
    settings: Settings,
    fake_transcriber: FakeTranscriber,
    wav_factory: Callable[[float], bytes],
) -> None:
    limited_settings = settings.model_copy(update={"max_media_duration_seconds": 0.1})
    limited_app = create_app(limited_settings, transcriber_factory=lambda _: fake_transcriber)

    with TestClient(limited_app) as limited_client:
        response = limited_client.post(
            "/v1/transcriptions",
            files={"file": ("sample.wav", wav_factory(0.25), "audio/wav")},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "media_too_long"
    assert fake_transcriber.requested_languages == []


def test_model_failure_is_sanitized(
    settings: Settings,
    wav_factory: Callable[[float], bytes],
) -> None:
    class FailingTranscriber(FakeTranscriber):
        def transcribe(
            self,
            media: BinaryIO,
            *,
            language: str | None,
        ) -> TranscriptionResult:
            raise RuntimeError("sensitive internal model error")

    app = create_app(settings, transcriber_factory=lambda _: FailingTranscriber())
    with TestClient(app) as client:
        response = client.post(
            "/v1/transcriptions",
            files={"file": ("sample.wav", wav_factory(0.25), "audio/wav")},
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "transcription_failed"
    assert "sensitive" not in response.text


def test_missing_file_uses_stable_validation_error(client: TestClient) -> None:
    response = client.post("/v1/transcriptions")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"

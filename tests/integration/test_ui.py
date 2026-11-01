"""Browser interface integration tests."""

from fastapi.testclient import TestClient

from tests.conftest import FakeTranscriber, make_wav_bytes
from transcription_service.core.config import Settings
from transcription_service.main import create_app


def test_root_serves_accessible_transcription_interface(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<html lang="en">' in response.text
    assert 'id="media-file"' in response.text
    assert 'name="language"' in response.text
    assert 'name="audio_mode"' in response.text
    assert 'name="model_profile"' in response.text
    assert 'id="transcript-text" dir="auto"' in response.text
    assert 'role="alert"' in response.text
    assert "/assets/app.css" in response.text
    assert "/assets/app.js" in response.text


def test_static_assets_are_served_same_origin(client: TestClient) -> None:
    stylesheet = client.get("/assets/app.css")
    script = client.get("/assets/app.js")

    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert "@media (max-width: 560px)" in stylesheet.text
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert "formData" in script.text
    assert "navigator.clipboard.writeText" in script.text
    assert "URL.createObjectURL" in script.text


def test_browser_configuration_follows_custom_api_prefix(settings: Settings) -> None:
    settings = settings.model_copy(update={"api_prefix": "/api/v2"})
    with TestClient(
        create_app(settings, transcriber_factory=lambda _: FakeTranscriber())
    ) as client:
        config = client.get("/client-config").json()
        assert config == {
            "transcription_url": "/api/v2/transcriptions",
            "max_upload_bytes": settings.max_upload_bytes,
        }
        response = client.post(
            config["transcription_url"],
            files={"file": ("sample.wav", make_wav_bytes(), "audio/wav")},
        )
        assert response.status_code == 200

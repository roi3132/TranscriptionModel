"""Browser interface integration tests."""

from fastapi.testclient import TestClient


def test_root_serves_accessible_transcription_interface(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<html lang="en">' in response.text
    assert 'id="media-file"' in response.text
    assert 'name="language"' in response.text
    assert 'name="audio_mode"' in response.text
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
    assert 'fetch("/v1/transcriptions"' in script.text
    assert "formData" in script.text
    assert "navigator.clipboard.writeText" in script.text
    assert "URL.createObjectURL" in script.text

"""Health endpoint integration tests."""

import re
from typing import NoReturn

from fastapi.testclient import TestClient

from tests.conftest import FakeTranscriber
from transcription_service.core.config import Settings
from transcription_service.main import create_app


def test_liveness_does_not_depend_on_model(client: TestClient) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
    assert re.fullmatch(r"[a-f0-9]{32}", response.headers["X-Request-ID"])


def test_readiness_fails_when_model_initialization_fails(settings: Settings) -> None:
    def fail_to_load(_: Settings) -> NoReturn:
        raise RuntimeError("model failed to load")

    failed_app = create_app(settings, transcriber_factory=fail_to_load)
    with TestClient(failed_app) as failed_client:
        response = failed_client.get(
            "/health/ready",
            headers={"X-Request-ID": "test-request-42"},
        )

    assert response.status_code == 503
    assert response.headers["X-Request-ID"] == "test-request-42"
    assert response.json() == {
        "error": {
            "code": "model_not_ready",
            "message": "The transcription model is not ready.",
            "request_id": "test-request-42",
        }
    }


def test_readiness_succeeds_after_model_load(client: TestClient) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_invalid_request_id_is_replaced(client: TestClient) -> None:
    response = client.get("/health/live", headers={"X-Request-ID": "not valid\n"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] != "not valid\n"
    assert re.fullmatch(r"[a-f0-9]{32}", response.headers["X-Request-ID"])


def test_http_errors_use_stable_envelope(client: TestClient) -> None:
    response = client.get("/does-not-exist", headers={"X-Request-ID": "missing-route"})

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "http_error",
            "message": "Not Found",
            "request_id": "missing-route",
        }
    }


def test_method_not_allowed_preserves_allow_header(client: TestClient) -> None:
    response = client.get("/v1/transcriptions")
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"


def test_unexpected_errors_keep_request_id_without_leaking_details(settings: Settings) -> None:
    app = create_app(settings, transcriber_factory=lambda _: FakeTranscriber())

    @app.get("/explode")
    async def explode() -> None:
        raise RuntimeError("private internal failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/explode", headers={"X-Request-ID": "unexpected-failure"})
    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "unexpected-failure"
    assert response.json()["error"]["request_id"] == "unexpected-failure"
    assert "private" not in response.text

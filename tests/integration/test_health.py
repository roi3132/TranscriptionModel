"""Health endpoint integration tests."""

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.job_fakes import MemoryJobRepository


def test_liveness_does_not_depend_on_model(client: TestClient) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
    assert re.fullmatch(r"[a-f0-9]{32}", response.headers["X-Request-ID"])


def test_readiness_fails_when_job_store_initialization_fails(
    app: FastAPI,
    job_repository: MemoryJobRepository,
) -> None:
    job_repository.ping_fails = True
    with TestClient(app) as failed_client:
        response = failed_client.get(
            "/health/ready",
            headers={"X-Request-ID": "test-request-42"},
        )

    assert response.status_code == 503
    assert response.headers["X-Request-ID"] == "test-request-42"
    assert response.json() == {
        "error": {
            "code": "job_service_not_ready",
            "message": "The transcription job service is not ready.",
            "request_id": "test-request-42",
        }
    }


def test_readiness_succeeds_when_job_service_is_ready(client: TestClient) -> None:
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


def test_unexpected_errors_keep_request_id_without_leaking_details(app: FastAPI) -> None:
    @app.get("/explode")
    async def explode() -> None:
        raise RuntimeError("private internal failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/explode", headers={"X-Request-ID": "unexpected-failure"})
    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "unexpected-failure"
    assert response.json()["error"]["request_id"] == "unexpected-failure"
    assert "private" not in response.text

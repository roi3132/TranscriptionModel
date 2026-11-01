"""Transport-level upload limits and multipart resource cleanup."""

import asyncio
from collections.abc import AsyncIterator
from tempfile import SpooledTemporaryFile
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeTranscriber, make_wav_bytes
from transcription_service.api.uploads import MULTIPART_OVERHEAD_BYTES
from transcription_service.core.config import Settings
from transcription_service.main import create_app


def test_rejects_oversized_declared_body_before_reading(settings: Settings) -> None:
    app = create_app(settings, transcriber_factory=lambda _: FakeTranscriber())
    consumed: list[bool] = []

    async def body() -> AsyncIterator[bytes]:
        consumed.append(True)
        yield b"unused"

    async def scenario() -> None:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/v1/transcriptions",
                content=body(),
                headers={
                    "Content-Length": str(settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES + 1)
                },
            )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "upload_too_large"

    asyncio.run(scenario())
    assert consumed == []


def test_chunked_upload_limit_closes_partial_files(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = settings.model_copy(update={"max_upload_bytes": 32})
    app = create_app(settings, transcriber_factory=lambda _: FakeTranscriber())
    opened: list[Any] = []
    consumed: list[int] = []

    def tracked_file(*args: Any, **kwargs: Any) -> Any:
        file = SpooledTemporaryFile(*args, **kwargs)  # noqa: SIM115 - the parser owns cleanup
        opened.append(file)
        return file

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", tracked_file)

    async def body() -> AsyncIterator[bytes]:
        yield (
            b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="sample.wav"'
            b"\r\nContent-Type: audio/wav\r\n\r\n"
        )
        yield b"a" * 1024
        yield b"b" * (MULTIPART_OVERHEAD_BYTES + 32)
        consumed.append(1)
        yield b"\r\n--boundary--\r\n"

    async def scenario() -> None:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/v1/transcriptions",
                content=body(),
                headers={"Content-Type": "multipart/form-data; boundary=boundary"},
            )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "upload_too_large"

    asyncio.run(scenario())
    assert opened and all(file.closed for file in opened)
    assert consumed == []


def test_rejects_extra_file_parts_before_inference(client: TestClient) -> None:
    response = client.post(
        "/v1/transcriptions",
        files=[
            ("file", ("first.wav", make_wav_bytes(), "audio/wav")),
            ("extra", ("second.wav", make_wav_bytes(), "audio/wav")),
        ],
    )
    assert response.status_code == 400


def test_upload_admission_limit_applies_before_body_is_read(settings: Settings) -> None:
    settings = settings.model_copy(update={"max_concurrent_uploads": 1})
    app = create_app(settings, transcriber_factory=lambda _: FakeTranscriber())

    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        second_consumed: list[bool] = []

        async def first_body() -> AsyncIterator[bytes]:
            yield (
                b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="sample.wav"'
                b"\r\nContent-Type: audio/wav\r\n\r\n"
            )
            started.set()
            await release.wait()
            yield make_wav_bytes()
            yield b"\r\n--boundary--\r\n"

        async def second_body() -> AsyncIterator[bytes]:
            second_consumed.append(True)
            yield b"unused"

        async with (
            app.router.lifespan_context(app),
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app), base_url="http://testserver"
            ) as client,
        ):
            first = asyncio.create_task(
                client.post(
                    "/v1/transcriptions",
                    content=first_body(),
                    headers={"Content-Type": "multipart/form-data; boundary=boundary"},
                )
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            try:
                rejected = await client.post("/v1/transcriptions", content=second_body())
                assert rejected.status_code == 503
                assert rejected.json()["error"]["code"] == "upload_busy"
                assert rejected.headers["Retry-After"] == "1"
                assert second_consumed == []
            finally:
                release.set()
                completed = await first
            assert completed.status_code == 200

    asyncio.run(scenario())

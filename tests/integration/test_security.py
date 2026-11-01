"""Regressions for browser boundaries, private storage, and child isolation."""

import asyncio
import io
import json
import os
import stat
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from tempfile import SpooledTemporaryFile
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_wav_bytes
from tests.job_fakes import MemoryJobBroker, MemoryJobRepository
from transcription_service.adapters.jobs import LocalMediaStore
from transcription_service.attempt import _write_result
from transcription_service.core.config import Settings
from transcription_service.jobs.domain import JobExecutionResult, TranscriptionJob
from transcription_service.main import create_app
from transcription_service.services.execution import IsolatedJobRunner
from transcription_service.services.transcription import TranscriptionService


@pytest.mark.parametrize("origin", ["https://attacker.example", "null"])
def test_foreign_browser_origin_cannot_submit(client: TestClient, origin: str) -> None:
    response = client.post(
        "/v1/transcriptions",
        headers={"Origin": origin},
        files={"file": ("test.wav", make_wav_bytes(), "audio/wav")},
    )
    assert response.status_code == 403


def test_rebinding_host_is_rejected_and_urls_are_relative(client: TestClient) -> None:
    assert client.get("/client-config", headers={"Host": "attacker.example"}).status_code == 400
    accepted = client.post(
        "/v1/transcriptions",
        headers={"Origin": "http://testserver"},
        files={"file": ("test.wav", make_wav_bytes(), "audio/wav")},
    )
    assert accepted.status_code == 202
    assert accepted.json()["status_url"].startswith("/v1/transcriptions/")
    assert accepted.json()["result_url"].startswith("/v1/transcriptions/")
    assert (
        client.post(
            "/v1/transcriptions",
            headers={"Sec-Fetch-Site": "cross-site"},
        ).status_code
        == 403
    )


def test_private_media_and_outputs_ignore_permissive_umask(tmp_path: Path) -> None:
    old_umask = os.umask(0)
    try:
        store = LocalMediaStore(tmp_path / "media")
        store.save("recording.media", io.BytesIO(b"secret"), max_total_bytes=100)
        checkpoint = tmp_path / "checkpoint.json"
        TranscriptionService._save_checkpoint(checkpoint, {})
        result = tmp_path / "result.json"
        _write_result(result, JobExecutionResult.succeeded({}))
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE((tmp_path / "media").stat().st_mode) == 0o700
    for path in (store.path_for("recording.media"), checkpoint, result):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_result_writer_refuses_existing_symlink(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("private")
    result = tmp_path / "result.json"
    result.with_suffix(".tmp").symlink_to(victim)
    with pytest.raises(FileExistsError):
        _write_result(result, JobExecutionResult.succeeded({}))
    assert victim.read_text() == "private"


def test_supervisor_refuses_symlink_and_fifo_results(tmp_path: Path) -> None:
    target = tmp_path / "private.json"
    target.write_text('{"result":{"text":"secret"}}')
    link = tmp_path / "link.json"
    link.symlink_to(target)
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    for path in (link, fifo):
        assert IsolatedJobRunner._read_result(path).error_code == "worker_result_invalid"


def test_attempt_gets_only_claimed_media_and_no_service_secrets(
    client: TestClient,
    job_repository: MemoryJobRepository,
    media_store: LocalMediaStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted = client.post(
        "/v1/transcriptions",
        files={"file": ("test.wav", make_wav_bytes(), "audio/wav")},
    ).json()
    job = job_repository.get(accepted["id"])
    assert job is not None
    monkeypatch.setenv("TRANSCRIPTION_DATABASE_URL", "postgresql://private:secret@private/db")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "private")

    def command(_: TranscriptionJob, result: Path) -> list[str]:
        code = (
            "import json,os,pathlib,stat; p=pathlib.Path.cwd(); "
            "m=json.loads((p/'input.json').read_text()); "
            "assert not any(k in os.environ for k in "
            "['TRANSCRIPTION_DATABASE_URL','AWS_SECRET_ACCESS_KEY']); "
            "assert 'database_url' not in m['settings']; "
            "assert stat.S_IMODE(p.stat().st_mode)==0o700; "
            "assert (p/'input.media').read_bytes(); "
            "(p/'input.chunks.json').write_text('{}'); "
            "(p/'result.json').write_text(json.dumps({'result': {'text':'ok'}}))"
        )
        return [sys.executable, "-c", code]

    outcome = IsolatedJobRunner(5, command_factory=command).run(
        job,
        media_store.path_for(job.media_key),
        lambda: None,
    )
    assert outcome.result == {"text": "ok"}
    checkpoint = media_store.path_for(job.media_key).with_suffix(".chunks.json")
    assert json.loads(checkpoint.read_text()) == {}
    assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600


def test_slow_upload_deadline_closes_partial_file_and_releases_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        log_level="CRITICAL",
        upload_timeout_seconds=0.05,
        max_concurrent_uploads=1,
    )
    opened: list[Any] = []

    def tracked(*args: Any, **kwargs: Any) -> Any:
        file = SpooledTemporaryFile(*args, **kwargs)  # noqa: SIM115
        opened.append(file)
        return file

    monkeypatch.setattr("starlette.formparsers.SpooledTemporaryFile", tracked)
    app = create_app(
        settings,
        job_repository=MemoryJobRepository(),
        job_broker=MemoryJobBroker(),
        media_store=LocalMediaStore(tmp_path / "media"),
    )

    async def body() -> AsyncIterator[bytes]:
        yield (
            b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="test.wav"'
            b"\r\nContent-Type: audio/wav\r\n\r\naudio"
        )
        await asyncio.sleep(1)
        yield b"\r\n--boundary--\r\n"

    async def scenario() -> None:
        async with (
            app.router.lifespan_context(app),
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app),
                base_url="http://testserver",
            ) as http,
        ):
            response = await http.post(
                "/v1/transcriptions",
                content=body(),
                headers={"Content-Type": "multipart/form-data; boundary=boundary"},
            )
            assert response.status_code == 408
            assert app.state.upload_slots.borrowed_tokens == 0

    asyncio.run(scenario())
    assert opened and all(file.closed for file in opened)

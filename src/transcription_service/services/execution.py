"""Inline and hard-deadline execution strategies for one job attempt."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from time import monotonic

import structlog

from transcription_service.core.errors import ApplicationError, ModelInferenceError
from transcription_service.jobs.domain import (
    JobExecutionResult,
    TranscriptionJob,
)
from transcription_service.schemas.transcription import (
    TranscriptionResponse,
    TranscriptionSegment,
)
from transcription_service.services.transcription import TranscriptionService

logger = structlog.get_logger(__name__)
AttemptCommandFactory = Callable[[TranscriptionJob, Path], list[str]]


class InlineJobRunner:
    """Run through an injected service; useful for fast deterministic tests."""

    def __init__(self, transcription_service: TranscriptionService) -> None:
        self._transcription_service = transcription_service

    def run(
        self,
        job: TranscriptionJob,
        media_path: Path,
        heartbeat: Callable[[], None],
    ) -> JobExecutionResult:
        del heartbeat
        try:
            response = asyncio.run(self._transcribe(job, media_path))
            return JobExecutionResult.succeeded(response.model_dump(mode="json"))
        except ApplicationError as exc:
            return JobExecutionResult.failed(
                code=exc.code,
                message=exc.client_message,
                retryable=isinstance(exc, ModelInferenceError),
            )
        except Exception as exc:
            logger.exception(
                "job_attempt_failed_unexpectedly",
                job_id=job.id,
                exception_type=type(exc).__name__,
            )
            return JobExecutionResult.failed(
                code="transcription_failed",
                message="The transcription could not be completed.",
                retryable=True,
            )

    async def _transcribe(self, job: TranscriptionJob, path: Path) -> TranscriptionResponse:
        with path.open("rb") as media:
            completed = await self._transcription_service.transcribe(
                media,
                size_bytes=path.stat().st_size,
                filename=job.filename,
                content_type=job.content_type,
                language=job.language,
                audio_mode=job.audio_mode,
                model_profile=job.model_profile,
            )
        return TranscriptionResponse(
            text=completed.result.text,
            language=completed.result.language,
            language_probability=completed.result.language_probability,
            duration_seconds=completed.result.duration_seconds,
            processing_time_seconds=completed.processing_time_seconds,
            model=completed.model_id,
            segments=[
                TranscriptionSegment(start=item.start, end=item.end, text=item.text)
                for item in completed.result.segments
            ],
        )


def _default_attempt_command(job: TranscriptionJob, result_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "transcription_service.attempt",
        job.id,
        str(result_path),
    ]


class IsolatedJobRunner:
    """Run each native inference attempt in a terminable child process."""

    def __init__(
        self,
        timeout_seconds: float,
        *,
        command_factory: AttemptCommandFactory = _default_attempt_command,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._command_factory = command_factory

    def run(
        self,
        job: TranscriptionJob,
        media_path: Path,
        heartbeat: Callable[[], None],
    ) -> JobExecutionResult:
        del media_path  # The child resolves the generated media key through MediaStore.
        file_descriptor, raw_result_path = tempfile.mkstemp(prefix="job-attempt-", suffix=".json")
        os.close(file_descriptor)
        result_path = Path(raw_result_path)
        result_path.unlink(missing_ok=True)
        process = subprocess.Popen(self._command_factory(job, result_path))
        deadline = monotonic() + self._timeout_seconds
        try:
            while process.poll() is None:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    self._stop(process)
                    return JobExecutionResult.failed(
                        code="job_timeout",
                        message="The transcription attempt exceeded its execution deadline.",
                        retryable=True,
                    )
                try:
                    process.wait(timeout=min(1.0, remaining))
                except subprocess.TimeoutExpired:
                    heartbeat()

            if process.returncode != 0 or not result_path.exists():
                return JobExecutionResult.failed(
                    code="worker_process_failed",
                    message="The isolated transcription process stopped unexpectedly.",
                    retryable=True,
                )
            return self._read_result(result_path)
        except Exception:
            if process.poll() is None:
                self._stop(process)
            raise
        finally:
            result_path.unlink(missing_ok=True)

    @staticmethod
    def _stop(process: subprocess.Popen[bytes]) -> None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    @staticmethod
    def _read_result(path: Path) -> JobExecutionResult:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload["result"] is not None:
                return JobExecutionResult.succeeded(payload["result"])
            return JobExecutionResult.failed(
                code=str(payload["error_code"]),
                message=str(payload["error_message"]),
                retryable=bool(payload["retryable"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return JobExecutionResult.failed(
                code="worker_result_invalid",
                message="The isolated transcription process returned an invalid result.",
                retryable=True,
            )

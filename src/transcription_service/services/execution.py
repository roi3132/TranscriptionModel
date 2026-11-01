"""Inline and hard-deadline execution strategies for one job attempt."""

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from time import monotonic

import structlog
from pydantic import TypeAdapter

from transcription_service.core.config import Settings
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
            response = self._transcribe(job, media_path)
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

    def _transcribe(self, job: TranscriptionJob, path: Path) -> TranscriptionResponse:
        with path.open("rb") as media:
            completed = self._transcription_service.transcribe(
                media,
                size_bytes=path.stat().st_size,
                filename=job.filename,
                content_type=job.content_type,
                language=job.language,
                audio_mode=job.audio_mode,
                checkpoint_path=path.with_suffix(".chunks.json"),
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
            warnings=list(completed.warnings),
        )


def _default_attempt_command(job: TranscriptionJob, result_path: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "transcription_service.attempt",
        str(result_path.parent / "input.json"),
        str(result_path),
    ]


class IsolatedJobRunner:
    """Run each native inference attempt in a terminable child process."""

    def __init__(
        self,
        timeout_seconds: float,
        *,
        command_factory: AttemptCommandFactory = _default_attempt_command,
        settings: Settings | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._command_factory = command_factory
        self._settings = settings or Settings()

    def run(
        self,
        job: TranscriptionJob,
        media_path: Path,
        heartbeat: Callable[[], None],
    ) -> JobExecutionResult:
        with tempfile.TemporaryDirectory(prefix="job-attempt-") as temporary:
            workspace = Path(temporary).resolve()
            private_media = workspace / "input.media"
            self._private_copy(media_path, private_media)
            checkpoint = media_path.with_suffix(".chunks.json")
            private_checkpoint = private_media.with_suffix(".chunks.json")
            if checkpoint.exists():
                self._private_copy(checkpoint, private_checkpoint)
            result_path = workspace / "result.json"
            manifest = {
                "job": json.loads(TypeAdapter(TranscriptionJob).dump_json(job)),
                "settings": self._settings.model_dump(
                    mode="json",
                    include={
                        "model_id",
                        "model_revision",
                        "larger_model_id",
                        "larger_model_revision",
                        "device",
                        "compute_type",
                        "beam_size",
                        "max_upload_bytes",
                        "max_media_duration_seconds",
                        "chunk_duration_seconds",
                        "chunk_max_attempts",
                        "job_timeout_seconds",
                        "attempt_memory_bytes",
                        "log_level",
                    },
                ),
            }
            descriptor = os.open(
                workspace / "input.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w") as output:
                json.dump(manifest, output)
            environment = {
                "PATH": os.defpath,
                "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "HF_HOME": str(
                    Path(os.environ.get("HF_HOME", Path.home() / ".cache/huggingface")).resolve()
                ),
                "TMPDIR": str(workspace),
                "HOME": str(workspace),
                "OMP_NUM_THREADS": "2",
            }
            try:
                deadline = monotonic() + self._timeout_seconds
                if self._command_factory is _default_attempt_command:
                    prepared = self._run_process(
                        job,
                        workspace / "prepared.json",
                        environment,
                        heartbeat,
                        command=[
                            sys.executable,
                            "-m",
                            "transcription_service.attempt",
                            "--prepare",
                            str(workspace / "input.json"),
                        ],
                        deadline=deadline,
                    )
                    if prepared.result is None:
                        return prepared
                    manifest["model_path"] = prepared.result["model_path"]
                    (workspace / "input.json").write_text(json.dumps(manifest))
                return self._run_process(
                    job, result_path, environment, heartbeat, deadline=deadline
                )
            finally:
                if private_checkpoint.is_file() and not private_checkpoint.is_symlink():
                    staged = checkpoint.with_suffix(".json.secure")
                    try:
                        self._private_copy(private_checkpoint, staged)
                        os.replace(staged, checkpoint)
                    finally:
                        staged.unlink(missing_ok=True)

    @staticmethod
    def _private_copy(source: Path, destination: Path) -> None:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with source.open("rb") as input_file, os.fdopen(descriptor, "wb") as output:
            shutil.copyfileobj(input_file, output)

    def _run_process(
        self,
        job: TranscriptionJob,
        result_path: Path,
        environment: dict[str, str],
        heartbeat: Callable[[], None],
        *,
        command: list[str] | None = None,
        deadline: float,
    ) -> JobExecutionResult:
        process = subprocess.Popen(
            command or self._command_factory(job, result_path),
            env=environment,
            cwd=result_path.parent,
            start_new_session=True,
            close_fds=True,
        )
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
            # Also stop descendants after a normal child exit.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)

    @staticmethod
    def _stop(process: subprocess.Popen[bytes]) -> None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    @staticmethod
    def _read_result(path: Path) -> JobExecutionResult:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "r", encoding="utf-8") as source:
                metadata = os.fstat(source.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 64 * 1024**2:
                    raise ValueError("Invalid result file")
                payload = json.load(source)
            if payload["result"] is not None:
                return JobExecutionResult.succeeded(payload["result"])
            return JobExecutionResult.failed(
                code=str(payload["error_code"]),
                message=str(payload["error_message"]),
                retryable=bool(payload["retryable"]),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return JobExecutionResult.failed(
                code="worker_result_invalid",
                message="The isolated transcription process returned an invalid result.",
                retryable=True,
            )

"""Child-process entry point for one bounded transcription attempt."""

import json
import os
import sys
from pathlib import Path

from pydantic import TypeAdapter

from transcription_service.core.config import Settings
from transcription_service.core.logging import configure_logging
from transcription_service.core.sandbox import restrict_attempt
from transcription_service.domain import ModelProfile
from transcription_service.jobs.domain import JobExecutionResult, TranscriptionJob


def _write_result(path: Path, result: JobExecutionResult) -> None:
    payload = {
        "result": result.result,
        "error_code": result.error_code,
        "error_message": result.error_message,
        "retryable": result.retryable,
    }
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(payload, output)
    os.replace(temporary, path)


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    os.umask(0o077)
    preparing = sys.argv[1] == "--prepare"
    manifest_path = Path(sys.argv[2] if preparing else sys.argv[1])
    manifest = json.loads(manifest_path.read_text())
    result_path = manifest_path.parent / "prepared.json" if preparing else Path(sys.argv[2])
    options = {"_env_file": None, "database_url": "", "redis_url": "", **manifest["settings"]}
    settings = Settings(**options)
    configure_logging(settings.log_level)
    job = TypeAdapter(TranscriptionJob).validate_python(manifest["job"])

    model_settings = settings
    if job.model_profile is ModelProfile.LARGER:
        model_settings = settings.model_copy(
            update={
                "model_id": settings.larger_model_id,
                "model_revision": settings.larger_model_revision,
            }
        )
    if preparing:
        # This separate process downloads only the operator-selected pinned model.
        # It exits before a fresh decoder process starts its native thread pools.
        from faster_whisper.utils import download_model

        model_path = download_model(model_settings.model_id, revision=model_settings.model_revision)
        _write_result(result_path, JobExecutionResult.succeeded({"model_path": model_path}))
        return 0

    model_path = Path(manifest["model_path"]).resolve()
    restrict_attempt(
        result_path.parent,
        model_path,
        memory_bytes=settings.attempt_memory_bytes,
        cpu_seconds=settings.job_timeout_seconds,
    )
    # Import native libraries only after restrictions are active: every thread
    # created by the ML/decoder libraries must inherit the sandbox.
    from transcription_service.adapters.faster_whisper import FasterWhisperTranscriber
    from transcription_service.adapters.media import PyAvMediaNormalizer
    from transcription_service.services.execution import InlineJobRunner
    from transcription_service.services.transcription import TranscriptionService

    transcriber = FasterWhisperTranscriber.from_settings(model_settings, model_path=str(model_path))
    service = TranscriptionService(
        transcriber,
        PyAvMediaNormalizer(
            max_duration_seconds=settings.max_media_duration_seconds,
            chunk_duration_seconds=settings.chunk_duration_seconds,
        ),
        settings,
    )
    outcome = InlineJobRunner(service).run(job, result_path.parent / "input.media", lambda: None)
    _write_result(result_path, outcome)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Child-process entry point for one bounded transcription attempt."""

import json
import os
import sys
from pathlib import Path

from transcription_service.adapters.faster_whisper import FasterWhisperTranscriber
from transcription_service.adapters.jobs import LocalMediaStore, PostgresJobRepository
from transcription_service.adapters.media import PyAvMediaNormalizer
from transcription_service.core.config import get_settings
from transcription_service.core.logging import configure_logging
from transcription_service.domain import ModelProfile
from transcription_service.jobs.domain import JobExecutionResult
from transcription_service.services.execution import InlineJobRunner
from transcription_service.services.transcription import TranscriptionService


def _write_result(path: Path, result: JobExecutionResult) -> None:
    payload = {
        "result": result.result,
        "error_code": result.error_code,
        "error_message": result.error_message,
        "retryable": result.retryable,
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    job_id = sys.argv[1]
    result_path = Path(sys.argv[2])
    settings = get_settings()
    configure_logging(settings.log_level)
    repository = PostgresJobRepository(settings.database_url)
    job = repository.get(job_id)
    if job is None:
        _write_result(
            result_path,
            JobExecutionResult.failed(
                code="job_not_found",
                message="The transcription job was not found.",
                retryable=False,
            ),
        )
        return 0

    model_settings = settings
    if job.model_profile is ModelProfile.LARGER:
        model_settings = settings.model_copy(
            update={
                "model_id": settings.larger_model_id,
                "model_revision": settings.larger_model_revision,
            }
        )
    transcriber = FasterWhisperTranscriber.from_settings(model_settings)
    service = TranscriptionService(
        transcriber,
        PyAvMediaNormalizer(
            max_duration_seconds=settings.max_media_duration_seconds,
            chunk_duration_seconds=settings.chunk_duration_seconds,
        ),
        settings,
    )
    media_store = LocalMediaStore(settings.media_storage_path)
    outcome = InlineJobRunner(service).run(job, media_store.path_for(job.media_key), lambda: None)
    _write_result(result_path, outcome)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

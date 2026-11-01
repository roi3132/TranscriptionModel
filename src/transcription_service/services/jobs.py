"""Submission and worker orchestration for durable transcription jobs."""

import asyncio
from functools import partial
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

import anyio
import structlog

from transcription_service.adapters.jobs import new_job
from transcription_service.core.errors import ApplicationError, JobInfrastructureError
from transcription_service.domain import AudioMode, ModelProfile
from transcription_service.jobs.domain import JobBroker, JobRepository, MediaStore, TranscriptionJob
from transcription_service.schemas.transcription import TranscriptionResponse, TranscriptionSegment
from transcription_service.services.transcription import TranscriptionService

logger = structlog.get_logger(__name__)


class JobSubmissionService:
    """Persist an upload and its job before handing the job to Redis."""

    def __init__(
        self,
        repository: JobRepository,
        broker: JobBroker,
        media_store: MediaStore,
    ) -> None:
        self.repository = repository
        self.broker = broker
        self.media_store = media_store

    async def submit(
        self,
        media: BinaryIO,
        *,
        filename: str | None,
        content_type: str | None,
        language: str | None,
        audio_mode: AudioMode,
        model_profile: ModelProfile,
    ) -> TranscriptionJob:
        job_id = str(uuid4())
        media_key = f"{job_id}.media"
        job = new_job(
            job_id=job_id,
            media_key=media_key,
            filename=filename,
            content_type=content_type,
            language=language,
            audio_mode=audio_mode,
            model_profile=model_profile,
        )
        persisted = False
        try:
            await anyio.to_thread.run_sync(self.media_store.save, media_key, media)
            await anyio.to_thread.run_sync(self.repository.create, job)
            persisted = True
            await anyio.to_thread.run_sync(self.broker.enqueue, job_id)
        except Exception as exc:
            if persisted:
                await anyio.to_thread.run_sync(
                    partial(
                        self.repository.mark_failed,
                        job_id,
                        code="job_enqueue_failed",
                        message="The transcription job could not be queued.",
                    )
                )
            await anyio.to_thread.run_sync(self.media_store.delete, media_key)
            await logger.aexception("job_submission_failed", exception_type=type(exc).__name__)
            raise JobInfrastructureError from exc
        return job

    async def get(self, job_id: str) -> TranscriptionJob | None:
        return await anyio.to_thread.run_sync(self.repository.get, job_id)


class TranscriptionJobWorker:
    """Consume job IDs and run the existing transcription use case outside HTTP."""

    def __init__(
        self,
        repository: JobRepository,
        broker: JobBroker,
        media_store: MediaStore,
        transcription_service: TranscriptionService,
    ) -> None:
        self._repository = repository
        self._broker = broker
        self._media_store = media_store
        self._transcription_service = transcription_service

    def run_forever(self) -> None:
        while True:
            job_id = self._broker.dequeue(timeout_seconds=5)
            if job_id is not None:
                self.process(job_id)

    def process(self, job_id: str) -> None:
        job = self._repository.get(job_id)
        if job is None or not self._repository.mark_running(job_id):
            return

        path = self._media_store.path_for(job.media_key)
        try:
            result = asyncio.run(self._transcribe(job, path))
            self._repository.mark_succeeded(job.id, result.model_dump(mode="json"))
            self._media_store.delete(job.media_key)
            logger.info("job_succeeded", job_id=job.id)
        except ApplicationError as exc:
            self._repository.mark_failed(
                job.id,
                code=exc.code,
                message=exc.client_message,
            )
            logger.warning("job_failed", job_id=job.id, error_code=exc.code)
        except Exception as exc:
            self._repository.mark_failed(
                job.id,
                code="transcription_failed",
                message="The transcription could not be completed.",
            )
            logger.exception(
                "job_failed_unexpectedly", job_id=job.id, exception_type=type(exc).__name__
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

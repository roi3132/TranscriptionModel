"""Submission, reliability policy, and worker orchestration for durable jobs."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from functools import partial
from time import monotonic
from typing import BinaryIO
from uuid import uuid4

import anyio
import structlog

from transcription_service.adapters.jobs import new_job
from transcription_service.core.config import Settings
from transcription_service.core.errors import (
    ApplicationError,
    IdempotencyConflictError,
    JobInfrastructureError,
    JobQueueFullError,
    JobServiceNotReadyError,
    MediaStorageFullError,
)
from transcription_service.domain import AudioMode, ModelProfile
from transcription_service.jobs.domain import (
    JobBroker,
    JobRepository,
    JobRunner,
    MediaStorageLimitReached,
    MediaStore,
    PendingJobLimitReached,
    SubmissionReason,
    TranscriptionJob,
)

logger = structlog.get_logger(__name__)


class JobSubmissionService:
    """Persist media and state before durable handoff, with deduplication."""

    def __init__(
        self,
        repository: JobRepository,
        broker: JobBroker,
        media_store: MediaStore,
        settings: Settings,
    ) -> None:
        self.repository = repository
        self.broker = broker
        self.media_store = media_store
        self._settings = settings

    async def submit(
        self,
        media: BinaryIO,
        *,
        filename: str | None,
        content_type: str | None,
        language: str | None,
        audio_mode: AudioMode,
        model_profile: ModelProfile,
        idempotency_key: str | None,
    ) -> TranscriptionJob:
        job_id = str(uuid4())
        media_key = f"{job_id}.media"
        persisted = False
        try:
            stored = await anyio.to_thread.run_sync(
                partial(
                    self.media_store.save,
                    media_key,
                    media,
                    max_total_bytes=self._settings.max_stored_media_bytes,
                )
            )
            model_revision = self._model_revision(model_profile)
            cache_key = self._cache_key(
                media_sha256=stored.sha256,
                model_revision=model_revision,
                language=language,
                audio_mode=audio_mode,
                model_profile=model_profile,
            )
            candidate = new_job(
                job_id=job_id,
                media_key=media_key,
                filename=filename,
                content_type=content_type,
                language=language,
                audio_mode=audio_mode,
                model_profile=model_profile,
                model_revision=model_revision,
                media_sha256=stored.sha256,
                cache_key=cache_key,
                idempotency_key=idempotency_key,
                size_bytes=stored.size_bytes,
                max_attempts=self._settings.job_max_attempts,
            )
            decision = await anyio.to_thread.run_sync(
                partial(
                    self.repository.create_or_reuse,
                    candidate,
                    max_pending_jobs=self._settings.max_pending_jobs,
                )
            )
            if decision.reason is not SubmissionReason.CREATED:
                await anyio.to_thread.run_sync(self.media_store.delete, media_key)
                if (
                    decision.reason is SubmissionReason.IDEMPOTENT
                    and decision.job.cache_key != candidate.cache_key
                ):
                    raise IdempotencyConflictError
                await logger.ainfo(
                    "job_submission_reused",
                    job_id=decision.job.id,
                    reason=decision.reason.value,
                )
                return decision.job

            persisted = True
            await anyio.to_thread.run_sync(self.broker.enqueue, job_id)
            return candidate
        except MediaStorageLimitReached as exc:
            raise MediaStorageFullError from exc
        except PendingJobLimitReached as exc:
            await anyio.to_thread.run_sync(self.media_store.delete, media_key)
            raise JobQueueFullError from exc
        except ApplicationError:
            raise
        except Exception as exc:
            if persisted:
                expires_at = datetime.now(UTC) + timedelta(
                    seconds=self._settings.failed_media_retention_seconds
                )
                await anyio.to_thread.run_sync(
                    partial(
                        self.repository.mark_failed,
                        job_id,
                        code="job_enqueue_failed",
                        message="The transcription job could not be queued.",
                        expires_at=expires_at,
                    )
                )
            else:
                await anyio.to_thread.run_sync(self.media_store.delete, media_key)
            await logger.aexception("job_submission_failed", exception_type=type(exc).__name__)
            raise JobInfrastructureError from exc

    async def get(self, job_id: str) -> TranscriptionJob | None:
        return await anyio.to_thread.run_sync(self.repository.get, job_id)

    async def check_ready(self) -> None:
        try:
            await anyio.to_thread.run_sync(self.repository.ping)
            await anyio.to_thread.run_sync(self.broker.ping)
            worker_ready = await anyio.to_thread.run_sync(self.broker.worker_is_ready)
        except Exception as exc:
            raise JobServiceNotReadyError from exc
        if not worker_ready:
            raise JobServiceNotReadyError

    def _model_revision(self, profile: ModelProfile) -> str:
        if profile is ModelProfile.FASTER:
            return self._settings.model_revision
        return self._settings.larger_model_revision

    @staticmethod
    def _cache_key(
        *,
        media_sha256: str,
        model_revision: str,
        language: str | None,
        audio_mode: AudioMode,
        model_profile: ModelProfile,
    ) -> str:
        identity = json.dumps(
            {
                "media_sha256": media_sha256,
                "model_revision": model_revision,
                "language": language or "auto",
                "audio_mode": audio_mode.value,
                "model_profile": model_profile.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode()).hexdigest()


class TranscriptionJobWorker:
    """Claim jobs atomically and apply retry, lease, and retention policies."""

    def __init__(
        self,
        repository: JobRepository,
        broker: JobBroker,
        media_store: MediaStore,
        runner: JobRunner,
        settings: Settings,
    ) -> None:
        self._repository = repository
        self._broker = broker
        self._media_store = media_store
        self._runner = runner
        self._settings = settings
        self._last_dispatch_scan = 0.0
        self._last_recovery_scan = 0.0
        self._last_cleanup = 0.0
        self._last_attempt_heartbeat = 0.0

    def run_forever(self) -> None:
        self.recover_stale()
        while True:
            self._broker.heartbeat(self._settings.worker_heartbeat_ttl_seconds)
            self.maintain()
            job_id = self._broker.dequeue(timeout_seconds=self._settings.worker_poll_seconds)
            if job_id is not None:
                self.process(job_id)

    def process(self, job_id: str) -> None:
        now = datetime.now(UTC)
        claimed = self._repository.claim(
            job_id,
            lease_expires_at=now + timedelta(seconds=self._settings.job_lease_seconds),
        )
        if claimed is None:
            return

        self._last_attempt_heartbeat = 0.0
        outcome = self._runner.run(
            claimed,
            self._media_store.path_for(claimed.media_key),
            partial(self._heartbeat_attempt, claimed.id),
        )
        completed_at = datetime.now(UTC)
        if outcome.result is not None:
            self._repository.mark_succeeded(
                claimed.id,
                outcome.result,
                expires_at=completed_at
                + timedelta(seconds=self._settings.result_retention_seconds),
            )
            try:
                self._media_store.delete(claimed.media_key)
            except OSError:
                logger.exception("completed_media_cleanup_failed", job_id=claimed.id)
            logger.info("job_succeeded", job_id=claimed.id, attempts=claimed.attempts)
            return

        error_code = outcome.error_code or "transcription_failed"
        error_message = outcome.error_message or "The transcription could not be completed."
        if outcome.retryable and claimed.attempts < claimed.max_attempts:
            delay = self._settings.job_retry_backoff_seconds * (2 ** (claimed.attempts - 1))
            available_at = completed_at + timedelta(seconds=delay)
            if self._repository.schedule_retry(
                claimed.id,
                code=error_code,
                message=error_message,
                available_at=available_at,
            ):
                logger.warning(
                    "job_retry_scheduled",
                    job_id=claimed.id,
                    attempt=claimed.attempts,
                    retry_in_seconds=delay,
                    error_code=error_code,
                )
            return

        self._repository.mark_failed(
            claimed.id,
            code=error_code,
            message=error_message,
            expires_at=completed_at
            + timedelta(seconds=self._settings.failed_media_retention_seconds),
        )
        logger.warning(
            "job_failed_terminally",
            job_id=claimed.id,
            attempts=claimed.attempts,
            error_code=error_code,
        )

    def recover_stale(self) -> None:
        now = datetime.now(UTC)
        recovered = self._repository.recover_stale(
            now=now,
            failed_expires_at=now
            + timedelta(seconds=self._settings.failed_media_retention_seconds),
        )
        for job_id in recovered:
            self._broker.enqueue(job_id)
        if recovered:
            logger.warning("stale_jobs_recovered", job_count=len(recovered))

    def maintain(self, *, force: bool = False) -> None:
        now_monotonic = monotonic()
        now = datetime.now(UTC)
        if force or now_monotonic - self._last_recovery_scan >= self._settings.worker_poll_seconds:
            self.recover_stale()
            self._last_recovery_scan = now_monotonic
        if force or now_monotonic - self._last_dispatch_scan >= self._settings.worker_poll_seconds:
            for job_id in self._repository.list_dispatchable(now=now, limit=100):
                self._broker.enqueue(job_id)
            self._last_dispatch_scan = now_monotonic
        if force or now_monotonic - self._last_cleanup >= self._settings.cleanup_interval_seconds:
            media_keys = self._repository.expire_due(now=now, limit=100)
            for media_key in media_keys:
                self._media_store.delete(media_key)
            if media_keys:
                logger.info("expired_jobs_cleaned", job_count=len(media_keys))
            self._last_cleanup = now_monotonic

    def _heartbeat_attempt(self, job_id: str) -> None:
        now_monotonic = monotonic()
        interval = max(1.0, self._settings.worker_heartbeat_ttl_seconds / 3)
        if now_monotonic - self._last_attempt_heartbeat < interval:
            return
        self._broker.heartbeat(self._settings.worker_heartbeat_ttl_seconds)
        self._repository.renew_lease(
            job_id,
            lease_expires_at=datetime.now(UTC)
            + timedelta(seconds=self._settings.job_lease_seconds),
        )
        self._last_attempt_heartbeat = now_monotonic

"""Thread-safe in-memory job ports shared by fast integration tests."""

from collections import deque
from dataclasses import replace
from datetime import UTC, datetime
from threading import Lock

from transcription_service.jobs.domain import (
    JobStatus,
    PendingJobLimitReached,
    SubmissionDecision,
    SubmissionReason,
    TranscriptionJob,
)


class MemoryJobRepository:
    def __init__(self) -> None:
        self.jobs: dict[str, TranscriptionJob] = {}
        self.idempotency_keys: dict[str, str] = {}
        self._lock = Lock()
        self.ping_fails = False

    def migrate(self) -> None:
        return None

    def ping(self) -> None:
        if self.ping_fails:
            raise ConnectionError("database unavailable")

    def create_or_reuse(
        self,
        job: TranscriptionJob,
        *,
        max_pending_jobs: int,
    ) -> SubmissionDecision:
        with self._lock:
            if job.idempotency_key is not None:
                existing_id = self.idempotency_keys.get(job.idempotency_key)
                if existing_id is not None:
                    return SubmissionDecision(
                        self.jobs[existing_id],
                        SubmissionReason.IDEMPOTENT,
                    )
            for existing in reversed(tuple(self.jobs.values())):
                active = existing.status in {JobStatus.QUEUED, JobStatus.RUNNING}
                cached = existing.status is JobStatus.SUCCEEDED and (
                    existing.expires_at is None or existing.expires_at > datetime.now(UTC)
                )
                if existing.cache_key == job.cache_key and (active or cached):
                    reason = (
                        SubmissionReason.CACHED_RESULT
                        if cached
                        else SubmissionReason.ACTIVE_DUPLICATE
                    )
                    if job.idempotency_key is not None:
                        self.idempotency_keys[job.idempotency_key] = existing.id
                    return SubmissionDecision(existing, reason)
            active_count = sum(
                item.status in {JobStatus.QUEUED, JobStatus.RUNNING} for item in self.jobs.values()
            )
            if active_count >= max_pending_jobs:
                raise PendingJobLimitReached
            self.jobs[job.id] = job
            if job.idempotency_key is not None:
                self.idempotency_keys[job.idempotency_key] = job.id
            return SubmissionDecision(job, SubmissionReason.CREATED)

    def get(self, job_id: str) -> TranscriptionJob | None:
        with self._lock:
            return self.jobs.get(job_id)

    def claim(self, job_id: str, *, lease_expires_at: datetime) -> TranscriptionJob | None:
        with self._lock:
            job = self.jobs.get(job_id)
            if (
                job is None
                or job.status is not JobStatus.QUEUED
                or job.available_at > datetime.now(UTC)
            ):
                return None
            claimed = replace(
                job,
                status=JobStatus.RUNNING,
                attempts=job.attempts + 1,
                lease_expires_at=lease_expires_at,
                error_code=None,
                error_message=None,
                updated_at=datetime.now(UTC),
            )
            self.jobs[job_id] = claimed
            return claimed

    def renew_lease(self, job_id: str, *, lease_expires_at: datetime) -> bool:
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None or job.status is not JobStatus.RUNNING:
                return False
            self.jobs[job_id] = replace(
                job,
                lease_expires_at=lease_expires_at,
                updated_at=datetime.now(UTC),
            )
            return True

    def mark_succeeded(
        self,
        job_id: str,
        result: dict[str, object],
        *,
        expires_at: datetime,
    ) -> None:
        with self._lock:
            job = self.jobs[job_id]
            self.jobs[job_id] = replace(
                job,
                status=JobStatus.SUCCEEDED,
                result=result,
                error_code=None,
                error_message=None,
                lease_expires_at=None,
                expires_at=expires_at,
                updated_at=datetime.now(UTC),
            )

    def mark_failed(
        self,
        job_id: str,
        *,
        code: str,
        message: str,
        expires_at: datetime,
    ) -> None:
        with self._lock:
            job = self.jobs[job_id]
            self.jobs[job_id] = replace(
                job,
                status=JobStatus.FAILED,
                error_code=code,
                error_message=message,
                lease_expires_at=None,
                expires_at=expires_at,
                updated_at=datetime.now(UTC),
            )

    def schedule_retry(
        self,
        job_id: str,
        *,
        code: str,
        message: str,
        available_at: datetime,
    ) -> bool:
        with self._lock:
            job = self.jobs[job_id]
            if job.status is not JobStatus.RUNNING:
                return False
            self.jobs[job_id] = replace(
                job,
                status=JobStatus.QUEUED,
                error_code=code,
                error_message=message,
                available_at=available_at,
                lease_expires_at=None,
                updated_at=datetime.now(UTC),
            )
            return True

    def recover_stale(
        self,
        *,
        now: datetime,
        failed_expires_at: datetime,
    ) -> list[str]:
        recovered: list[str] = []
        with self._lock:
            for job_id, job in tuple(self.jobs.items()):
                if (
                    job.status is not JobStatus.RUNNING
                    or job.lease_expires_at is None
                    or job.lease_expires_at > now
                ):
                    continue
                if job.attempts >= job.max_attempts:
                    self.jobs[job_id] = replace(
                        job,
                        status=JobStatus.FAILED,
                        error_code="worker_crash",
                        error_message="The worker stopped before completing the job.",
                        lease_expires_at=None,
                        expires_at=failed_expires_at,
                    )
                else:
                    self.jobs[job_id] = replace(
                        job,
                        status=JobStatus.QUEUED,
                        error_code="worker_crash",
                        error_message="Retrying after a worker interruption.",
                        lease_expires_at=None,
                        available_at=now,
                    )
                    recovered.append(job_id)
        return recovered

    def list_dispatchable(self, *, now: datetime, limit: int) -> list[str]:
        with self._lock:
            return [
                job.id
                for job in self.jobs.values()
                if job.status is JobStatus.QUEUED and job.available_at <= now
            ][:limit]

    def expire_due(self, *, now: datetime, limit: int) -> list[str]:
        media_keys: list[str] = []
        with self._lock:
            for job_id, job in tuple(self.jobs.items()):
                if len(media_keys) >= limit:
                    break
                if (
                    job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED}
                    and job.expires_at is not None
                    and job.expires_at <= now
                ):
                    self.jobs[job_id] = replace(
                        job,
                        status=JobStatus.EXPIRED,
                        result=None,
                        updated_at=now,
                    )
                    media_keys.append(job.media_key)
        return media_keys


class MemoryJobBroker:
    def __init__(self) -> None:
        self.jobs: deque[str] = deque()
        self.ready = False
        self.ping_fails = False

    def ping(self) -> None:
        if self.ping_fails:
            raise ConnectionError("broker unavailable")

    def enqueue(self, job_id: str) -> None:
        self.jobs.append(job_id)

    def dequeue(self, timeout_seconds: int) -> str | None:
        del timeout_seconds
        return self.jobs.popleft() if self.jobs else None

    def heartbeat(self, ttl_seconds: int) -> None:
        del ttl_seconds
        self.ready = True

    def worker_is_ready(self) -> bool:
        return self.ready

    def close(self) -> None:
        return None

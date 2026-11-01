"""Domain types and ports for durable transcription jobs."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Protocol

from transcription_service.domain import AudioMode, ModelProfile


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"


class SubmissionReason(StrEnum):
    CREATED = "created"
    IDEMPOTENT = "idempotent"
    ACTIVE_DUPLICATE = "active_duplicate"
    CACHED_RESULT = "cached_result"


@dataclass(frozen=True, slots=True)
class StoredMedia:
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class TranscriptionJob:
    id: str
    status: JobStatus
    media_key: str
    filename: str | None
    content_type: str | None
    language: str | None
    audio_mode: AudioMode
    model_profile: ModelProfile
    model_revision: str
    media_sha256: str
    cache_key: str
    idempotency_key: str | None
    size_bytes: int
    attempts: int
    max_attempts: int
    result: dict[str, object] | None
    error_code: str | None
    error_message: str | None
    available_at: datetime
    lease_expires_at: datetime | None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SubmissionDecision:
    job: TranscriptionJob
    reason: SubmissionReason


@dataclass(frozen=True, slots=True)
class JobExecutionResult:
    result: dict[str, object] | None
    error_code: str | None
    error_message: str | None
    retryable: bool

    @classmethod
    def succeeded(cls, result: dict[str, object]) -> "JobExecutionResult":
        return cls(result=result, error_code=None, error_message=None, retryable=False)

    @classmethod
    def failed(
        cls,
        *,
        code: str,
        message: str,
        retryable: bool,
    ) -> "JobExecutionResult":
        return cls(
            result=None,
            error_code=code,
            error_message=message,
            retryable=retryable,
        )


class PendingJobLimitReached(Exception):
    """Raised by a repository when admission would exceed its active-job limit."""


class MediaStorageLimitReached(Exception):
    """Raised by a media store when an upload would exceed its storage budget."""


class JobRepository(Protocol):
    def migrate(self) -> None: ...

    def ping(self) -> None: ...

    def create_or_reuse(
        self,
        job: TranscriptionJob,
        *,
        max_pending_jobs: int,
    ) -> SubmissionDecision: ...

    def get(self, job_id: str) -> TranscriptionJob | None: ...

    def claim(self, job_id: str, *, lease_expires_at: datetime) -> TranscriptionJob | None: ...

    def renew_lease(self, job_id: str, *, lease_expires_at: datetime) -> bool: ...

    def mark_succeeded(
        self,
        job_id: str,
        result: dict[str, object],
        *,
        expires_at: datetime,
    ) -> None: ...

    def mark_failed(
        self,
        job_id: str,
        *,
        code: str,
        message: str,
        expires_at: datetime,
    ) -> None: ...

    def schedule_retry(
        self,
        job_id: str,
        *,
        code: str,
        message: str,
        available_at: datetime,
    ) -> bool: ...

    def recover_stale(
        self,
        *,
        now: datetime,
        failed_expires_at: datetime,
    ) -> list[str]: ...

    def list_dispatchable(self, *, now: datetime, limit: int) -> list[str]: ...

    def expire_due(self, *, now: datetime, limit: int) -> list[str]: ...


class JobBroker(Protocol):
    def ping(self) -> None: ...

    def enqueue(self, job_id: str) -> None: ...

    def dequeue(self, timeout_seconds: int) -> str | None: ...

    def heartbeat(self, ttl_seconds: int) -> None: ...

    def worker_is_ready(self) -> bool: ...

    def close(self) -> None: ...


class MediaStore(Protocol):
    def save(
        self,
        key: str,
        source: BinaryIO,
        *,
        max_total_bytes: int,
    ) -> StoredMedia: ...

    def path_for(self, key: str) -> Path: ...

    def delete(self, key: str) -> None: ...


class JobRunner(Protocol):
    def run(
        self,
        job: TranscriptionJob,
        media_path: Path,
        heartbeat: Callable[[], None],
    ) -> JobExecutionResult: ...

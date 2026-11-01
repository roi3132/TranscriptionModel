"""Domain types and ports for durable transcription jobs."""

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
    result: dict[str, object] | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class JobRepository(Protocol):
    def migrate(self) -> None: ...

    def ping(self) -> None: ...

    def create(self, job: TranscriptionJob) -> None: ...

    def get(self, job_id: str) -> TranscriptionJob | None: ...

    def mark_running(self, job_id: str) -> bool: ...

    def mark_succeeded(self, job_id: str, result: dict[str, object]) -> None: ...

    def mark_failed(self, job_id: str, *, code: str, message: str) -> None: ...


class JobBroker(Protocol):
    def ping(self) -> None: ...

    def enqueue(self, job_id: str) -> None: ...

    def dequeue(self, timeout_seconds: int) -> str | None: ...

    def close(self) -> None: ...


class MediaStore(Protocol):
    def save(self, key: str, source: BinaryIO) -> None: ...

    def path_for(self, key: str) -> Path: ...

    def delete(self, key: str) -> None: ...

"""HTTP schemas for durable transcription jobs."""

from datetime import datetime

from pydantic import BaseModel

from transcription_service.jobs.domain import JobStatus
from transcription_service.schemas.transcription import TranscriptionResponse


class JobAcceptedResponse(BaseModel):
    id: str
    status: JobStatus
    status_url: str
    result_url: str


class JobStatusResponse(BaseModel):
    id: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    error_code: str | None = None
    error_message: str | None = None
    attempts: int
    max_attempts: int
    next_retry_at: datetime | None = None
    expires_at: datetime | None = None
    result_url: str | None = None


class JobResultResponse(TranscriptionResponse):
    job_id: str

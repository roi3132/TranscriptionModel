"""Synchronous media transcription endpoint."""

import re
from functools import partial
from http import HTTPStatus
from typing import Annotated, BinaryIO
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse

from transcription_service.api.uploads import BoundedUploadRoute
from transcription_service.core.errors import (
    EmptyUploadError,
    InvalidIdempotencyKeyError,
    InvalidLanguageError,
    JobNotFoundError,
    JobResultNotReadyError,
    ModelNotReadyError,
    UploadTooLargeError,
)
from transcription_service.domain import AudioMode, ModelProfile
from transcription_service.jobs.domain import JobStatus
from transcription_service.schemas.common import ErrorResponse
from transcription_service.schemas.jobs import (
    JobAcceptedResponse,
    JobResultResponse,
    JobStatusResponse,
)
from transcription_service.schemas.transcription import (
    TranscriptionResponse,
    TranscriptionSegment,
)
from transcription_service.services.jobs import JobSubmissionService
from transcription_service.services.transcription import TranscriptionService

router = APIRouter(
    prefix="/transcriptions", tags=["transcriptions"], route_class=BoundedUploadRoute
)
LANGUAGE_CODE_PATTERN = re.compile(r"^[a-z]{2,3}$")
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def transcription_service(request: Request) -> TranscriptionService | None:
    """Resolve the process-wide service created by the application lifespan."""

    service: TranscriptionService | None = request.app.state.transcription_service
    if service is None and request.app.state.job_service is None:
        raise ModelNotReadyError
    return service


TranscriptionServiceDependency = Annotated[
    TranscriptionService | None,
    Depends(transcription_service),
]


def _job_service(request: Request) -> JobSubmissionService | None:
    service: JobSubmissionService | None = request.app.state.job_service
    return service


def _canonical_job_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise JobNotFoundError from exc


def _normalized_language(value: str) -> str | None:
    normalized = value.strip().lower()
    if normalized == "auto":
        return None
    if not LANGUAGE_CODE_PATTERN.fullmatch(normalized):
        raise InvalidLanguageError(normalized or value)
    return normalized


def _normalized_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(normalized):
        raise InvalidIdempotencyKeyError
    return normalized


def _stream_size(media: BinaryIO) -> int:
    media.seek(0, 2)
    size = media.tell()
    media.seek(0)
    return size


@router.post(
    "",
    response_model=TranscriptionResponse | JobAcceptedResponse,
    responses={
        HTTPStatus.BAD_REQUEST: {"model": ErrorResponse},
        HTTPStatus.REQUEST_ENTITY_TOO_LARGE: {"model": ErrorResponse},
        HTTPStatus.UNSUPPORTED_MEDIA_TYPE: {"model": ErrorResponse},
        HTTPStatus.UNPROCESSABLE_ENTITY: {"model": ErrorResponse},
        HTTPStatus.INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
        HTTPStatus.SERVICE_UNAVAILABLE: {"model": ErrorResponse},
        HTTPStatus.CONFLICT: {"model": ErrorResponse},
        HTTPStatus.INSUFFICIENT_STORAGE: {"model": ErrorResponse},
        HTTPStatus.ACCEPTED: {"model": JobAcceptedResponse},
    },
    summary="Transcribe an audio or video file",
)
async def create_transcription(
    request: Request,
    file: Annotated[UploadFile, File(description="Audio or video file to transcribe")],
    service: TranscriptionServiceDependency,
    language: Annotated[
        str,
        Form(description="Whisper language code or 'auto' for detection"),
    ] = "auto",
    audio_mode: Annotated[
        AudioMode,
        Form(description="Use 'speech' for VAD or 'music' to process the full audio track"),
    ] = AudioMode.SPEECH,
    model_profile: Annotated[
        ModelProfile,
        Form(description="'faster' uses the small model; 'larger' lazily loads large-v3"),
    ] = ModelProfile.FASTER,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> TranscriptionResponse | JSONResponse:
    """Complete synchronously or accept a durable job, according to deployment mode."""

    # BoundedUploadRoute and FastAPI own the multipart form's complete lifetime.
    size_bytes = file.size
    if size_bytes is None:
        size_bytes = await anyio.to_thread.run_sync(partial(_stream_size, file.file))
    if size_bytes == 0:
        raise EmptyUploadError
    if size_bytes > request.app.state.settings.max_upload_bytes:
        raise UploadTooLargeError(request.app.state.settings.max_upload_bytes)

    normalized_language = _normalized_language(language)
    job_service = _job_service(request)
    if job_service is not None:
        job = await job_service.submit(
            file.file,
            filename=file.filename,
            content_type=file.content_type,
            language=normalized_language,
            audio_mode=audio_mode,
            model_profile=model_profile,
            idempotency_key=_normalized_idempotency_key(idempotency_key),
        )
        status_url = str(request.url_for("get_transcription_job", job_id=job.id))
        result_url = str(request.url_for("get_transcription_result", job_id=job.id))
        payload = JobAcceptedResponse(
            id=job.id,
            status=job.status,
            status_url=status_url,
            result_url=result_url,
        )
        return JSONResponse(
            status_code=HTTPStatus.ACCEPTED,
            content=payload.model_dump(mode="json"),
            headers={"Location": status_url},
        )

    if service is None:
        raise ModelNotReadyError
    completed = await service.transcribe(
        file.file,
        size_bytes=size_bytes,
        filename=file.filename,
        content_type=file.content_type,
        language=normalized_language,
        audio_mode=audio_mode,
        model_profile=model_profile,
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


@router.get(
    "/{job_id}",
    response_model=JobStatusResponse,
    responses={HTTPStatus.NOT_FOUND: {"model": ErrorResponse}},
    name="get_transcription_job",
)
async def get_transcription_job(job_id: str, request: Request) -> JobStatusResponse:
    service = _job_service(request)
    if service is None:
        raise JobNotFoundError
    job = await service.get(_canonical_job_id(job_id))
    if job is None:
        raise JobNotFoundError
    result_url = None
    if job.status is JobStatus.SUCCEEDED:
        result_url = str(request.url_for("get_transcription_result", job_id=job.id))
    return JobStatusResponse(
        id=job.id,
        status=job.status,
        created_at=job.created_at,
        updated_at=job.updated_at,
        error_code=job.error_code if job.status is JobStatus.FAILED else None,
        error_message=job.error_message if job.status is JobStatus.FAILED else None,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        next_retry_at=(
            job.available_at if job.status is JobStatus.QUEUED and job.attempts > 0 else None
        ),
        expires_at=job.expires_at,
        result_url=result_url,
    )


@router.get(
    "/{job_id}/result",
    response_model=JobResultResponse,
    responses={
        HTTPStatus.NOT_FOUND: {"model": ErrorResponse},
        HTTPStatus.CONFLICT: {"model": ErrorResponse},
    },
    name="get_transcription_result",
)
async def get_transcription_result(job_id: str, request: Request) -> JobResultResponse:
    service = _job_service(request)
    if service is None:
        raise JobNotFoundError
    job = await service.get(_canonical_job_id(job_id))
    if job is None:
        raise JobNotFoundError
    if job.status is not JobStatus.SUCCEEDED or job.result is None:
        raise JobResultNotReadyError
    return JobResultResponse.model_validate({**job.result, "job_id": job.id})

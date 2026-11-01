"""Durable asynchronous transcription endpoints."""

import re
from functools import partial
from http import HTTPStatus
from typing import Annotated, BinaryIO
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, Response, UploadFile

from transcription_service.api.uploads import BoundedUploadRoute
from transcription_service.core.errors import (
    EmptyUploadError,
    InvalidIdempotencyKeyError,
    InvalidLanguageError,
    JobNotFoundError,
    JobResultNotReadyError,
    JobServiceNotReadyError,
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
from transcription_service.services.exports import ResultFormat, render_result
from transcription_service.services.jobs import JobSubmissionService

router = APIRouter(
    prefix="/transcriptions", tags=["transcriptions"], route_class=BoundedUploadRoute
)
LANGUAGE_CODE_PATTERN = re.compile(r"^[a-z]{2,3}$")
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def job_service(request: Request) -> JobSubmissionService:
    """Resolve the durable job service created by the application lifespan."""

    service: JobSubmissionService | None = request.app.state.job_service
    if service is None:
        raise JobServiceNotReadyError
    return service


JobServiceDependency = Annotated[JobSubmissionService, Depends(job_service)]


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
    status_code=HTTPStatus.ACCEPTED,
    response_model=JobAcceptedResponse,
    responses={
        HTTPStatus.BAD_REQUEST: {"model": ErrorResponse},
        HTTPStatus.REQUEST_ENTITY_TOO_LARGE: {"model": ErrorResponse},
        HTTPStatus.UNPROCESSABLE_ENTITY: {"model": ErrorResponse},
        HTTPStatus.TOO_MANY_REQUESTS: {"model": ErrorResponse},
        HTTPStatus.SERVICE_UNAVAILABLE: {"model": ErrorResponse},
        HTTPStatus.CONFLICT: {"model": ErrorResponse},
        HTTPStatus.INSUFFICIENT_STORAGE: {"model": ErrorResponse},
    },
    summary="Queue an audio or video transcription",
)
async def create_transcription(
    request: Request,
    response: Response,
    file: Annotated[UploadFile, File(description="Audio or video file to transcribe")],
    service: JobServiceDependency,
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
) -> JobAcceptedResponse:
    """Persist an upload and return the durable job used to process it."""

    # BoundedUploadRoute and FastAPI own the multipart form's complete lifetime.
    size_bytes = file.size
    if size_bytes is None:
        size_bytes = await anyio.to_thread.run_sync(partial(_stream_size, file.file))
    if size_bytes == 0:
        raise EmptyUploadError
    if size_bytes > request.app.state.settings.max_upload_bytes:
        raise UploadTooLargeError(request.app.state.settings.max_upload_bytes)

    job = await service.submit(
        file.file,
        filename=file.filename,
        content_type=file.content_type,
        language=_normalized_language(language),
        audio_mode=audio_mode,
        model_profile=model_profile,
        idempotency_key=_normalized_idempotency_key(idempotency_key),
    )
    status_url = request.url_for("get_transcription_job", job_id=job.id).path
    response.headers["Location"] = status_url
    return JobAcceptedResponse(
        id=job.id,
        status=job.status,
        status_url=status_url,
        result_url=request.url_for("get_transcription_result", job_id=job.id).path,
    )


@router.get(
    "/{job_id}",
    response_model=JobStatusResponse,
    responses={HTTPStatus.NOT_FOUND: {"model": ErrorResponse}},
    name="get_transcription_job",
)
async def get_transcription_job(
    job_id: str,
    request: Request,
    service: JobServiceDependency,
) -> JobStatusResponse:
    job = await service.get(_canonical_job_id(job_id))
    if job is None:
        raise JobNotFoundError
    result_url = None
    if job.status is JobStatus.SUCCEEDED:
        result_url = request.url_for("get_transcription_result", job_id=job.id).path
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
        HTTPStatus.OK: {
            "content": {
                "application/json": {},
                "text/plain": {},
                "application/x-subrip": {},
                "text/vtt": {},
            }
        },
        HTTPStatus.NOT_FOUND: {"model": ErrorResponse},
        HTTPStatus.CONFLICT: {"model": ErrorResponse},
    },
    name="get_transcription_result",
)
async def get_transcription_result(
    job_id: str,
    service: JobServiceDependency,
    result_format: Annotated[ResultFormat, Query(alias="format")] = ResultFormat.JSON,
) -> JobResultResponse | Response:
    job = await service.get(_canonical_job_id(job_id))
    if job is None:
        raise JobNotFoundError
    if job.status is not JobStatus.SUCCEEDED or job.result is None:
        raise JobResultNotReadyError
    result = JobResultResponse.model_validate({**job.result, "job_id": job.id})
    if result_format is ResultFormat.JSON:
        return result
    media_types = {
        ResultFormat.TXT: "text/plain",
        ResultFormat.SRT: "application/x-subrip",
        ResultFormat.VTT: "text/vtt",
    }
    return Response(
        render_result(result, result_format),
        media_type=media_types[result_format],
        headers={"Content-Disposition": f'attachment; filename="{job.id}.{result_format.value}"'},
    )

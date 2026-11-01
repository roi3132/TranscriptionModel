"""Synchronous transcription endpoint."""

import re
from functools import partial
from http import HTTPStatus
from typing import Annotated, BinaryIO

import anyio
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from transcription_service.core.errors import InvalidLanguageError, ModelNotReadyError
from transcription_service.schemas.common import ErrorResponse
from transcription_service.schemas.transcription import (
    TranscriptionResponse,
    TranscriptionSegment,
)
from transcription_service.services.transcription import TranscriptionService

router = APIRouter(prefix="/transcriptions", tags=["transcriptions"])
LANGUAGE_CODE_PATTERN = re.compile(r"^[a-z]{2,3}$")


def transcription_service(request: Request) -> TranscriptionService:
    """Resolve the process-wide service created by the application lifespan."""

    service: TranscriptionService | None = request.app.state.transcription_service
    if service is None:
        raise ModelNotReadyError
    return service


TranscriptionServiceDependency = Annotated[
    TranscriptionService,
    Depends(transcription_service),
]


def _normalized_language(value: str) -> str | None:
    normalized = value.strip().lower()
    if normalized == "auto":
        return None
    if not LANGUAGE_CODE_PATTERN.fullmatch(normalized):
        raise InvalidLanguageError(normalized or value)
    return normalized


def _stream_size(media: BinaryIO) -> int:
    media.seek(0, 2)
    size = media.tell()
    media.seek(0)
    return size


@router.post(
    "",
    response_model=TranscriptionResponse,
    responses={
        HTTPStatus.BAD_REQUEST: {"model": ErrorResponse},
        HTTPStatus.REQUEST_ENTITY_TOO_LARGE: {"model": ErrorResponse},
        HTTPStatus.UNSUPPORTED_MEDIA_TYPE: {"model": ErrorResponse},
        HTTPStatus.UNPROCESSABLE_ENTITY: {"model": ErrorResponse},
        HTTPStatus.INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
        HTTPStatus.SERVICE_UNAVAILABLE: {"model": ErrorResponse},
    },
    summary="Transcribe an audio file",
)
async def create_transcription(
    file: Annotated[UploadFile, File(description="Audio file to transcribe")],
    service: TranscriptionServiceDependency,
    language: Annotated[
        str,
        Form(description="Whisper language code or 'auto' for detection"),
    ] = "auto",
) -> TranscriptionResponse:
    """Return a completed, timestamped transcription for one audio upload."""

    try:
        size_bytes = file.size
        if size_bytes is None:
            size_bytes = await anyio.to_thread.run_sync(partial(_stream_size, file.file))

        completed = await service.transcribe(
            file.file,
            size_bytes=size_bytes,
            filename=file.filename,
            content_type=file.content_type,
            language=_normalized_language(language),
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
    finally:
        await file.close()

"""Liveness and readiness probes."""

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from transcription_service.core.errors import ModelNotReadyError
from transcription_service.core.status import RuntimeStatus
from transcription_service.schemas.common import ErrorResponse, HealthResponse

router = APIRouter(prefix="/health", tags=["health"])


def runtime_status(request: Request) -> RuntimeStatus:
    """Resolve process status from application state."""

    status: RuntimeStatus = request.app.state.runtime_status
    return status


RuntimeStatusDependency = Annotated[RuntimeStatus, Depends(runtime_status)]


@router.get("/live", response_model=HealthResponse, summary="Process liveness")
async def liveness() -> HealthResponse:
    """Report that the HTTP process is running without touching the model."""

    return HealthResponse(status="alive")


@router.get(
    "/ready",
    response_model=HealthResponse,
    responses={HTTPStatus.SERVICE_UNAVAILABLE: {"model": ErrorResponse}},
    summary="Inference readiness",
)
async def readiness(status: RuntimeStatusDependency) -> HealthResponse:
    """Report whether a transcription model can accept work."""

    if not status.model_ready:
        raise ModelNotReadyError
    return HealthResponse(status="ready")

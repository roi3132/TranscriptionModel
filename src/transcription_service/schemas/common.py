"""Schemas shared by multiple endpoints."""

from typing import Literal

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    """Stable, client-safe error details."""

    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    """Envelope used by every application and HTTP error."""

    error: ErrorDetail


class HealthResponse(BaseModel):
    """Health probe response."""

    status: Literal["alive", "ready"]

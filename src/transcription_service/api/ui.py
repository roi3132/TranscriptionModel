"""Browser user interface routes."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from transcription_service.core.config import Settings

router = APIRouter(tags=["web"])
STATIC_DIRECTORY = Path(__file__).resolve().parent.parent / "static"


@router.get("/", include_in_schema=False)
async def transcription_page() -> FileResponse:
    """Serve the buildless browser interface."""

    return FileResponse(STATIC_DIRECTORY / "index.html")


@router.get("/client-config", include_in_schema=False)
async def client_configuration(request: Request) -> dict[str, str | int]:
    """Expose only the public settings needed by the browser."""

    settings: Settings = request.app.state.settings
    return {
        "transcription_url": f"{settings.api_prefix}/transcriptions",
        "max_upload_bytes": settings.max_upload_bytes,
    }

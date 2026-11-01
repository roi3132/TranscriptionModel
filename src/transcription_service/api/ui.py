"""Browser user interface routes."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["web"])
STATIC_DIRECTORY = Path(__file__).resolve().parent.parent / "static"


@router.get("/", include_in_schema=False)
async def transcription_page() -> FileResponse:
    """Serve the buildless browser interface."""

    return FileResponse(STATIC_DIRECTORY / "index.html")

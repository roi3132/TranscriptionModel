"""Public response schema tests."""

import pytest
from pydantic import ValidationError

from transcription_service.schemas.transcription import (
    TranscriptionResponse,
    TranscriptionSegment,
)


def test_transcription_schema_preserves_hebrew_text() -> None:
    response = TranscriptionResponse(
        text="שלום עולם",
        language="he",
        language_probability=0.98,
        duration_seconds=2.5,
        processing_time_seconds=0.9,
        model="Systran/faster-whisper-small",
        segments=[TranscriptionSegment(start=0, end=2.5, text="שלום עולם")],
    )

    assert response.model_dump(mode="json")["text"] == "שלום עולם"


def test_segment_rejects_reversed_timestamps() -> None:
    with pytest.raises(ValidationError):
        TranscriptionSegment(start=2, end=1, text="invalid")

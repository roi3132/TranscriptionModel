"""Transcription API response schemas."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TranscriptionSegment(BaseModel):
    """A timestamped span of transcribed text."""

    model_config = ConfigDict(str_strip_whitespace=True)

    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def end_must_not_precede_start(self) -> "TranscriptionSegment":
        if self.end < self.start:
            raise ValueError("segment end must be greater than or equal to its start")
        return self


class TranscriptionResponse(BaseModel):
    """Completed synchronous transcription."""

    model_config = ConfigDict(str_strip_whitespace=True)

    text: str
    language: str = Field(min_length=2)
    language_probability: float = Field(ge=0, le=1)
    duration_seconds: float = Field(ge=0)
    processing_time_seconds: float = Field(ge=0)
    model: str = Field(min_length=1)
    segments: list[TranscriptionSegment]

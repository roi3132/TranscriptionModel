"""Result rendering tests for each downloadable representation."""

from transcription_service.schemas.jobs import JobResultResponse
from transcription_service.schemas.transcription import TranscriptionSegment
from transcription_service.services.exports import ResultFormat, render_result


def _result() -> JobResultResponse:
    return JobResultResponse(
        job_id="job-1",
        text="Hello world",
        language="en",
        language_probability=0.98,
        duration_seconds=62.25,
        processing_time_seconds=1.2,
        model="test/model",
        segments=[
            TranscriptionSegment(start=0, end=1.5, text="Hello"),
            TranscriptionSegment(start=60.125, end=62.25, text="world"),
        ],
    )


def test_renders_plain_text() -> None:
    assert render_result(_result(), ResultFormat.TXT) == "Hello world\n"


def test_renders_numbered_srt_cues() -> None:
    rendered = render_result(_result(), ResultFormat.SRT)

    assert "1\n00:00:00,000 --> 00:00:01,500\nHello" in rendered
    assert "2\n00:01:00,125 --> 00:01:02,250\nworld" in rendered


def test_renders_webvtt_cues() -> None:
    rendered = render_result(_result(), ResultFormat.VTT)

    assert rendered.startswith("WEBVTT\n\n")
    assert "00:01:00.125 --> 00:01:02.250\nworld" in rendered

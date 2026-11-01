"""Tests for mapping faster-whisper output into application results."""

import io
from collections.abc import Iterable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from faster_whisper import WhisperModel

from transcription_service.adapters.faster_whisper import FasterWhisperTranscriber
from transcription_service.domain import AudioMode


@dataclass
class FakeSegment:
    start: float
    end: float
    text: str


class FakeWhisperModel:
    supported_languages: ClassVar[list[str]] = ["en", "he"]

    def __init__(self) -> None:
        self.generator_consumed = False
        self.received_language: str | None = None
        self.received_vad_filter: bool | None = None
        self.received_no_speech_threshold: float | str | None = "not-provided"

    def transcribe(self, media: Any, **options: Any) -> tuple[Iterable[FakeSegment], Any]:
        self.received_language = options["language"]
        self.received_vad_filter = options["vad_filter"]
        self.received_no_speech_threshold = options.get(
            "no_speech_threshold",
            "not-provided",
        )

        def segments() -> Iterable[FakeSegment]:
            self.generator_consumed = True
            yield FakeSegment(start=0, end=1.2, text=" שלום")
            yield FakeSegment(start=1.2, end=2.0, text=" עולם ")
            yield FakeSegment(start=2.0, end=2.1, text="   ")

        info = SimpleNamespace(language="he", language_probability=0.95, duration=2.1)
        return segments(), info


def test_eagerly_consumes_segments_and_maps_result() -> None:
    model = FakeWhisperModel()
    adapter = FasterWhisperTranscriber(
        cast(WhisperModel, model),
        model_id="test/model",
        beam_size=5,
    )

    result = adapter.transcribe(
        io.BytesIO(b"audio"),
        language="he",
        audio_mode=AudioMode.SPEECH,
    )

    assert model.generator_consumed
    assert model.received_language == "he"
    assert model.received_vad_filter is True
    assert model.received_no_speech_threshold == "not-provided"
    assert result.text == "שלום עולם"
    assert result.language == "he"
    assert result.language_probability == 0.95
    assert result.duration_seconds == 2.1
    assert [(item.start, item.end, item.text) for item in result.segments] == [
        (0, 1.2, "שלום"),
        (1.2, 2.0, "עולם"),
    ]
    assert adapter.supported_languages == frozenset({"en", "he"})


def test_music_mode_disables_voice_activity_detection() -> None:
    model = FakeWhisperModel()
    adapter = FasterWhisperTranscriber(
        cast(WhisperModel, model),
        model_id="test/model",
        beam_size=5,
    )

    adapter.transcribe(
        io.BytesIO(b"music"),
        language="he",
        audio_mode=AudioMode.MUSIC,
    )

    assert model.received_vad_filter is False
    assert model.received_no_speech_threshold is None


def test_segments_stay_within_the_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    model = FakeWhisperModel()

    def transcribe(media: Any, **options: Any) -> tuple[list[FakeSegment], Any]:
        return (
            [
                FakeSegment(-1, 1.2, "first"),
                FakeSegment(1.2, 30, "last"),
                FakeSegment(31, 32, "outside"),
                FakeSegment(1.5, 1.5, "zero length"),
            ],
            SimpleNamespace(language="en", language_probability=1, duration=2.0),
        )

    monkeypatch.setattr(model, "transcribe", transcribe)
    adapter = FasterWhisperTranscriber(cast(WhisperModel, model), model_id="test", beam_size=5)
    result = adapter.transcribe(io.BytesIO(b"audio"), language="en", audio_mode=AudioMode.MUSIC)
    assert result.text == "first last"
    assert [(segment.start, segment.end) for segment in result.segments] == [(0, 1.2), (1.2, 2)]

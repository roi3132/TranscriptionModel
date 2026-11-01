"""Transcription use-case tests."""

import asyncio
import io
from threading import Event
from typing import BinaryIO, ClassVar

import pytest

from transcription_service.core.config import Settings
from transcription_service.core.errors import ModelBusyError
from transcription_service.services.transcription import (
    AudioMode,
    MediaMetadata,
    SegmentResult,
    TranscriptionResult,
    TranscriptionService,
)


class StaticMediaInspector:
    def inspect(
        self,
        media: BinaryIO,
        *,
        filename: str | None,
        content_type: str | None,
    ) -> MediaMetadata:
        return MediaMetadata(duration_seconds=0.25, format_name="wav")


class BlockingTranscriber:
    model_id = "test/blocking-model"
    supported_languages: ClassVar[frozenset[str]] = frozenset({"en", "he"})

    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def transcribe(
        self,
        media: BinaryIO,
        *,
        language: str | None,
        audio_mode: AudioMode,
    ) -> TranscriptionResult:
        self.started.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("test did not release blocking transcriber")
        return TranscriptionResult(
            text="done",
            language=language or "en",
            language_probability=1,
            duration_seconds=0.25,
            segments=(SegmentResult(start=0, end=0.25, text="done"),),
        )


def test_rejects_second_request_when_inference_queue_times_out() -> None:
    async def scenario() -> None:
        settings = Settings(
            _env_file=None,
            environment="test",
            max_concurrent_transcriptions=1,
            inference_queue_timeout_seconds=0.01,
        )
        transcriber = BlockingTranscriber()
        service = TranscriptionService(transcriber, StaticMediaInspector(), settings)
        first_request = asyncio.create_task(
            service.transcribe(
                io.BytesIO(b"first"),
                size_bytes=5,
                filename="first.wav",
                content_type="audio/wav",
                language="en",
                audio_mode=AudioMode.SPEECH,
            )
        )

        started = await asyncio.to_thread(transcriber.started.wait, 1)
        assert started
        try:
            with pytest.raises(ModelBusyError) as raised:
                await service.transcribe(
                    io.BytesIO(b"second"),
                    size_bytes=6,
                    filename="second.wav",
                    content_type="audio/wav",
                    language="en",
                    audio_mode=AudioMode.SPEECH,
                )
            assert raised.value.headers == {"Retry-After": "1"}
        finally:
            transcriber.release.set()

        completed = await first_request
        assert completed.result.text == "done"

    asyncio.run(scenario())

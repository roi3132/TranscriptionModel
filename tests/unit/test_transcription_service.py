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
    ModelProfile,
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


@pytest.mark.parametrize("model_profile", list(ModelProfile))
def test_rejects_second_request_when_inference_queue_times_out(model_profile: ModelProfile) -> None:
    async def scenario() -> None:
        settings = Settings(
            _env_file=None,
            environment="test",
            max_concurrent_transcriptions=1,
            inference_queue_timeout_seconds=0.01,
        )
        transcriber = BlockingTranscriber()
        service = TranscriptionService(
            transcriber, StaticMediaInspector(), settings, transcriber_factory=lambda _: transcriber
        )
        first_request = asyncio.create_task(
            service.transcribe(
                io.BytesIO(b"first"),
                size_bytes=5,
                filename="first.wav",
                content_type="audio/wav",
                language="en",
                audio_mode=AudioMode.SPEECH,
                model_profile=model_profile,
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


def test_concurrent_large_requests_initialize_model_once() -> None:
    async def scenario() -> None:
        settings = Settings(_env_file=None, max_concurrent_transcriptions=2)
        transcriber = BlockingTranscriber()
        transcriber.release.set()
        loading_started = Event()
        finish_loading = Event()
        load_count = 0

        def factory(model_settings: Settings) -> BlockingTranscriber:
            nonlocal load_count
            load_count += 1
            assert model_settings.model_id == settings.larger_model_id
            loading_started.set()
            if not finish_loading.wait(timeout=2):
                raise TimeoutError("test did not release model loading")
            return transcriber

        service = TranscriptionService(
            transcriber, StaticMediaInspector(), settings, transcriber_factory=factory
        )

        async def request() -> str:
            completed = await service.transcribe(
                io.BytesIO(b"audio"),
                size_bytes=5,
                filename="sample.wav",
                content_type="audio/wav",
                language="he",
                audio_mode=AudioMode.MUSIC,
                model_profile=ModelProfile.LARGER,
            )
            return completed.result.text

        tasks = [asyncio.create_task(request()) for _ in range(2)]
        try:
            assert await asyncio.to_thread(loading_started.wait, 1)
        finally:
            finish_loading.set()
        assert await asyncio.gather(*tasks) == ["done", "done"]
        assert load_count == 1

    asyncio.run(scenario())

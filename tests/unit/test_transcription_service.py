"""Chunk retry, ordering, and checkpoint-resume tests."""

import io
from pathlib import Path
from typing import BinaryIO, ClassVar

import pytest

from transcription_service.core.config import Settings
from transcription_service.core.errors import ModelInferenceError
from transcription_service.domain import (
    AudioMode,
    NormalizedChunk,
    NormalizedMedia,
    SegmentResult,
    TranscriptionResult,
)
from transcription_service.services.transcription import TranscriptionService


class ChunkedMediaNormalizer:
    def normalize(
        self,
        media: BinaryIO,
        *,
        filename: str | None,
        content_type: str | None,
    ) -> NormalizedMedia:
        del media, filename, content_type
        return NormalizedMedia(
            chunks=tuple(
                NormalizedChunk(index, index * 0.25, io.BytesIO(str(index).encode()))
                for index in range(3)
            ),
            duration_seconds=0.75,
            is_silent=False,
        )


class ChunkTranscriber:
    model_id = "test/chunk-model"
    supported_languages: ClassVar[frozenset[str]] = frozenset({"en", "he"})

    def __init__(self, failures: dict[int, int] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[int] = []

    def transcribe(
        self,
        media: BinaryIO,
        *,
        language: str | None,
        audio_mode: AudioMode,
    ) -> TranscriptionResult:
        del audio_mode
        index = int(media.read().decode())
        self.calls.append(index)
        if self.failures.get(index, 0) > 0:
            self.failures[index] -= 1
            raise RuntimeError("planned chunk failure")
        text = f"chunk-{index}"
        return TranscriptionResult(
            text=text,
            language=language or "en",
            language_probability=0.9,
            duration_seconds=0.25,
            segments=(SegmentResult(0, 0.25, text),),
        )


def _transcribe(service: TranscriptionService, checkpoint: Path) -> TranscriptionResult:
    return service.transcribe(
        io.BytesIO(b"source"),
        size_bytes=6,
        filename="sample.wav",
        content_type="audio/wav",
        language="en",
        audio_mode=AudioMode.SPEECH,
        checkpoint_path=checkpoint,
    ).result


def test_chunk_retry_preserves_order_and_offsets_timestamps(tmp_path: Path) -> None:
    transcriber = ChunkTranscriber({1: 1})
    result = _transcribe(
        TranscriptionService(
            transcriber,
            ChunkedMediaNormalizer(),
            Settings(_env_file=None, chunk_max_attempts=2),
        ),
        tmp_path / "chunks.json",
    )

    assert transcriber.calls == [0, 1, 1, 2]
    assert result.text == "chunk-0 chunk-1 chunk-2"
    assert [segment.start for segment in result.segments] == [0, 0.25, 0.5]
    assert [segment.end for segment in result.segments] == [0.25, 0.5, 0.75]


def test_chunk_checkpoint_resumes_after_failed_attempt(tmp_path: Path) -> None:
    checkpoint = tmp_path / "chunks.json"
    settings = Settings(_env_file=None, chunk_max_attempts=2)
    failing = ChunkTranscriber({2: 2})

    with pytest.raises(ModelInferenceError):
        _transcribe(
            TranscriptionService(failing, ChunkedMediaNormalizer(), settings),
            checkpoint,
        )
    assert failing.calls == [0, 1, 2, 2]

    resumed = ChunkTranscriber()
    result = _transcribe(
        TranscriptionService(resumed, ChunkedMediaNormalizer(), settings),
        checkpoint,
    )

    assert resumed.calls == [2]
    assert result.text == "chunk-0 chunk-1 chunk-2"

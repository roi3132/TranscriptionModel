"""PyAV media normalization tests."""

import io
import wave
from collections.abc import Callable

import pytest

from tests.conftest import make_wav_bytes
from transcription_service.adapters.media import PyAvMediaNormalizer
from transcription_service.core.errors import (
    MediaTooLongError,
    NoAudioStreamError,
    UnsupportedMediaError,
)


def test_normalizes_to_16khz_mono_wav_and_leaves_source_open(
    wav_factory: Callable[[float], bytes],
) -> None:
    source = io.BytesIO(wav_factory(0.25))
    source.seek(7)

    normalized = PyAvMediaNormalizer().normalize(
        source,
        filename="sample.wav",
        content_type="audio/wav",
    )

    assert normalized.duration_seconds == pytest.approx(0.25)
    assert normalized.is_silent is False
    assert len(normalized.chunks) == 1
    with wave.open(normalized.chunks[0].media, "rb") as wav_file:
        assert wav_file.getframerate() == 16_000
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getnframes() == 4_000
    assert source.tell() == 0
    assert not source.closed


def test_splits_normalized_audio_into_ordered_chunks(
    wav_factory: Callable[[float], bytes],
) -> None:
    normalized = PyAvMediaNormalizer(chunk_duration_seconds=0.1).normalize(
        io.BytesIO(wav_factory(0.25)),
        filename="sample.wav",
        content_type="audio/wav",
    )

    assert [chunk.index for chunk in normalized.chunks] == [0, 1, 2]
    assert [chunk.start_seconds for chunk in normalized.chunks] == pytest.approx([0, 0.1, 0.2])


def test_marks_zero_pcm_as_silent() -> None:
    normalized = PyAvMediaNormalizer().normalize(
        io.BytesIO(make_wav_bytes(0.1, sample=0)),
        filename="silence.wav",
        content_type="audio/wav",
    )

    assert normalized.is_silent is True


@pytest.mark.parametrize(
    ("payload", "filename", "content_type"),
    [
        (b"plain text", "notes.txt", "text/plain"),
        (b"not wave data", "broken.wav", "audio/wav"),
    ],
)
def test_rejects_unsupported_or_corrupt_media(
    payload: bytes,
    filename: str,
    content_type: str,
) -> None:
    with pytest.raises(UnsupportedMediaError):
        PyAvMediaNormalizer().normalize(
            io.BytesIO(payload),
            filename=filename,
            content_type=content_type,
        )


def test_rejects_audio_container_without_samples(
    wav_factory: Callable[[float], bytes],
) -> None:
    with pytest.raises(UnsupportedMediaError):
        PyAvMediaNormalizer().normalize(
            io.BytesIO(wav_factory(0)),
            filename="empty.wav",
            content_type="audio/wav",
        )


@pytest.mark.parametrize(
    ("filename", "content_type", "container_format"),
    [
        ("sample.mp4", "video/mp4", "mp4"),
        ("sample.webm", "video/webm", "webm"),
    ],
)
def test_normalizes_video_audio_stream(
    video_with_audio_factory: Callable[[str, float], bytes],
    filename: str,
    content_type: str,
    container_format: str,
) -> None:
    normalized = PyAvMediaNormalizer().normalize(
        io.BytesIO(video_with_audio_factory(container_format, 0.5)),
        filename=filename,
        content_type=content_type,
    )

    assert normalized.duration_seconds == pytest.approx(0.5, abs=0.1)
    assert len(normalized.chunks) == 1


def test_rejects_video_without_audio(
    video_without_audio_factory: Callable[[str, float], bytes],
) -> None:
    with pytest.raises(NoAudioStreamError):
        PyAvMediaNormalizer().normalize(
            io.BytesIO(video_without_audio_factory("mp4", 0.25)),
            filename="silent.mp4",
            content_type="video/mp4",
        )


def test_decoded_duration_is_bounded(wav_factory: Callable[[float], bytes]) -> None:
    with pytest.raises(MediaTooLongError):
        PyAvMediaNormalizer(max_duration_seconds=0.1).normalize(
            io.BytesIO(wav_factory(0.25)),
            filename="sample.wav",
            content_type="audio/wav",
        )

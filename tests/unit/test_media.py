"""PyAV media inspection tests."""

import io
from collections.abc import Callable, Iterator
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from transcription_service.adapters.media import PyAvMediaInspector
from transcription_service.core.errors import (
    MediaTooLongError,
    NoAudioStreamError,
    UnsupportedMediaError,
)


def test_inspects_wav_and_leaves_stream_open(
    wav_factory: Callable[[float], bytes],
) -> None:
    media = io.BytesIO(wav_factory(0.25))
    media.seek(7)

    metadata = PyAvMediaInspector().inspect(
        media,
        filename="sample.wav",
        content_type="audio/wav",
    )

    assert metadata.format_name == "wav"
    assert metadata.duration_seconds == pytest.approx(0.25)
    assert media.tell() == 0
    assert not media.closed


def test_rejects_unknown_file_type() -> None:
    with pytest.raises(UnsupportedMediaError):
        PyAvMediaInspector().inspect(
            io.BytesIO(b"plain text"),
            filename="notes.txt",
            content_type="text/plain",
        )


def test_rejects_corrupt_allowed_container() -> None:
    with pytest.raises(UnsupportedMediaError):
        PyAvMediaInspector().inspect(
            io.BytesIO(b"not wave data"),
            filename="broken.wav",
            content_type="audio/wav",
        )


def test_rejects_audio_container_without_samples(wav_factory: Callable[[float], bytes]) -> None:
    with pytest.raises(UnsupportedMediaError):
        PyAvMediaInspector().inspect(
            io.BytesIO(wav_factory(0)), filename="empty.wav", content_type="audio/wav"
        )


@pytest.mark.parametrize(
    ("filename", "content_type", "container_format"),
    [
        ("sample.mp4", "video/mp4", "mp4"),
        ("sample.webm", "video/webm", "webm"),
    ],
)
def test_inspects_video_with_audio_and_resets_stream(
    video_with_audio_factory: Callable[[str, float], bytes],
    filename: str,
    content_type: str,
    container_format: str,
) -> None:
    media = io.BytesIO(video_with_audio_factory(container_format, 0.5))
    media.seek(11)

    metadata = PyAvMediaInspector().inspect(
        media,
        filename=filename,
        content_type=content_type,
    )

    assert metadata.duration_seconds == pytest.approx(0.5, abs=0.1)
    assert media.tell() == 0
    assert not media.closed


def test_rejects_video_without_audio(
    video_without_audio_factory: Callable[[str, float], bytes],
) -> None:
    with pytest.raises(NoAudioStreamError):
        PyAvMediaInspector().inspect(
            io.BytesIO(video_without_audio_factory("mp4", 0.25)),
            filename="silent.mp4",
            content_type="video/mp4",
        )


def test_rejects_corrupt_video_container() -> None:
    with pytest.raises(UnsupportedMediaError):
        PyAvMediaInspector().inspect(
            io.BytesIO(b"not an mp4"),
            filename="broken.mp4",
            content_type="video/mp4",
        )


@pytest.mark.parametrize("declared_duration", [None, 1])
def test_decoded_duration_is_bounded_even_with_missing_or_wrong_metadata(
    monkeypatch: pytest.MonkeyPatch, declared_duration: int | None
) -> None:
    stream = SimpleNamespace(duration=declared_duration, time_base=Fraction(1, 16000))
    container = MagicMock()
    container.__enter__.return_value = container
    container.streams.audio = [stream]
    container.duration = None
    decoded: list[int] = []

    def decode(_stream: object) -> Iterator[SimpleNamespace]:
        for index in range(100):
            decoded.append(index)
            yield SimpleNamespace(samples=16000, sample_rate=16000)

    container.decode.side_effect = decode
    monkeypatch.setattr("transcription_service.adapters.media.av.open", lambda *a, **kw: container)
    media = io.BytesIO(b"audio")
    with pytest.raises(MediaTooLongError):
        PyAvMediaInspector(max_duration_seconds=2.5).inspect(
            media, filename="sample.wav", content_type="audio/wav"
        )
    assert decoded == [0, 1, 2]
    assert media.tell() == 0
    assert not media.closed

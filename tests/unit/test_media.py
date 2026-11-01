"""PyAV media inspection tests."""

import io
from collections.abc import Callable

import pytest

from transcription_service.adapters.media import PyAvMediaInspector
from transcription_service.core.errors import NoAudioStreamError, UnsupportedMediaError


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

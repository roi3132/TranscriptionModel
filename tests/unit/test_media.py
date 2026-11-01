"""PyAV audio inspection tests."""

import io
from collections.abc import Callable

import pytest

from transcription_service.adapters.media import PyAvAudioInspector
from transcription_service.core.errors import UnsupportedMediaError


def test_inspects_wav_and_leaves_stream_open(
    wav_factory: Callable[[float], bytes],
) -> None:
    media = io.BytesIO(wav_factory(0.25))
    media.seek(7)

    metadata = PyAvAudioInspector().inspect(
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
        PyAvAudioInspector().inspect(
            io.BytesIO(b"plain text"),
            filename="notes.txt",
            content_type="text/plain",
        )


def test_rejects_corrupt_allowed_container() -> None:
    with pytest.raises(UnsupportedMediaError):
        PyAvAudioInspector().inspect(
            io.BytesIO(b"not wave data"),
            filename="broken.wav",
            content_type="audio/wav",
        )

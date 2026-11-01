"""Audio and video container inspection implemented with PyAV."""

from fractions import Fraction
from pathlib import Path
from typing import BinaryIO

import av

from transcription_service.core.errors import (
    MediaTooLongError,
    NoAudioStreamError,
    UnsupportedMediaError,
)
from transcription_service.domain import MediaMetadata

AUDIO_EXTENSIONS = frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"})
VIDEO_EXTENSIONS = frozenset({".mkv", ".mov", ".mp4", ".webm"})
AUDIO_CONTENT_TYPES = frozenset(
    {
        "application/ogg",
        "audio/aac",
        "audio/flac",
        "audio/mp4",
        "audio/mpeg",
        "audio/ogg",
        "audio/opus",
        "audio/wav",
        "audio/wave",
        "audio/x-flac",
        "audio/x-m4a",
        "audio/x-wav",
    }
)
VIDEO_CONTENT_TYPES = frozenset(
    {
        "application/x-matroska",
        "video/mp4",
        "video/quicktime",
        "video/webm",
        "video/x-matroska",
    }
)
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
MEDIA_CONTENT_TYPES = AUDIO_CONTENT_TYPES | VIDEO_CONTENT_TYPES


class PyAvMediaInspector:
    """Validate and bound decoded audio from the container's first audio stream."""

    def __init__(self, *, max_duration_seconds: float = 900) -> None:
        self._max_duration_seconds = max_duration_seconds

    def inspect(
        self,
        media: BinaryIO,
        *,
        filename: str | None,
        content_type: str | None,
    ) -> MediaMetadata:
        suffix = Path(filename or "").suffix.lower()
        normalized_content_type = (content_type or "").partition(";")[0].strip().lower()

        if suffix not in MEDIA_EXTENSIONS and normalized_content_type not in MEDIA_CONTENT_TYPES:
            raise UnsupportedMediaError

        try:
            media.seek(0)
            with av.open(media, mode="r") as container:
                if not container.streams.audio:
                    raise NoAudioStreamError

                audio_stream = container.streams.audio[0]
                duration_seconds: float | None = None
                if audio_stream.duration is not None and audio_stream.time_base is not None:
                    duration_seconds = float(audio_stream.duration * audio_stream.time_base)

                if duration_seconds is not None and duration_seconds > self._max_duration_seconds:
                    raise MediaTooLongError(self._max_duration_seconds)

                # Container metadata may be missing or inaccurate. Count decoded samples as well,
                # stopping before faster-whisper can allocate an oversized audio array.
                decoded_duration = Fraction(0)
                for frame in container.decode(audio_stream):
                    if frame.sample_rate <= 0:
                        raise UnsupportedMediaError
                    decoded_duration += Fraction(frame.samples, frame.sample_rate)
                    if decoded_duration > self._max_duration_seconds:
                        raise MediaTooLongError(self._max_duration_seconds)

                if not decoded_duration:
                    raise UnsupportedMediaError

                return MediaMetadata(
                    duration_seconds=float(decoded_duration),
                    format_name=container.format.name or "unknown",
                )
        except NoAudioStreamError:
            raise
        except (av.error.FFmpegError, EOFError, OSError, ValueError) as exc:
            raise UnsupportedMediaError from exc
        finally:
            if not media.closed:
                media.seek(0)

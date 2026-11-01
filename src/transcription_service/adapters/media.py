"""Audio and video container inspection implemented with PyAV."""

from pathlib import Path
from typing import BinaryIO

import av

from transcription_service.core.errors import NoAudioStreamError, UnsupportedMediaError
from transcription_service.services.transcription import MediaMetadata

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
    """Validate supported media and inspect its first audio stream."""

    def inspect(
        self,
        media: BinaryIO,
        *,
        filename: str | None,
        content_type: str | None,
    ) -> MediaMetadata:
        suffix = Path(filename or "").suffix.lower()
        normalized_content_type = (content_type or "").lower().split(";", maxsplit=1)[0]

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
                elif container.duration is not None:
                    duration_seconds = float(container.duration / av.time_base)

                return MediaMetadata(
                    duration_seconds=duration_seconds,
                    format_name=container.format.name or "unknown",
                )
        except NoAudioStreamError:
            raise
        except (av.error.FFmpegError, EOFError, OSError, ValueError) as exc:
            raise UnsupportedMediaError from exc
        finally:
            if not media.closed:
                media.seek(0)

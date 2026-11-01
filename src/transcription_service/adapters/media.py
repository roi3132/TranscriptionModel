"""Audio container inspection implemented with PyAV."""

from pathlib import Path
from typing import BinaryIO

import av

from transcription_service.core.errors import NoAudioStreamError, UnsupportedMediaError
from transcription_service.services.transcription import MediaMetadata

AUDIO_EXTENSIONS = frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"})
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


class PyAvAudioInspector:
    """Validate phase-1 audio uploads and read inexpensive container metadata."""

    def inspect(
        self,
        media: BinaryIO,
        *,
        filename: str | None,
        content_type: str | None,
    ) -> MediaMetadata:
        suffix = Path(filename or "").suffix.lower()
        normalized_content_type = (content_type or "").lower().split(";", maxsplit=1)[0]

        if normalized_content_type.startswith("video/"):
            raise UnsupportedMediaError
        if suffix not in AUDIO_EXTENSIONS and normalized_content_type not in AUDIO_CONTENT_TYPES:
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

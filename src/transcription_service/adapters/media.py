"""Explicit 16 kHz mono PCM normalization implemented with PyAV."""

import io
import wave
from itertools import chain
from pathlib import Path
from typing import BinaryIO

import av

from transcription_service.core.errors import (
    MediaTooLongError,
    NoAudioStreamError,
    UnsupportedMediaError,
)
from transcription_service.domain import NormalizedChunk, NormalizedMedia

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


class PyAvMediaNormalizer:
    """Decode the first audio stream into bounded 16 kHz mono WAV chunks."""

    sample_rate = 16_000
    sample_width = 2

    def __init__(
        self,
        *,
        max_duration_seconds: float = 900,
        chunk_duration_seconds: float = 300,
    ) -> None:
        self._max_duration_seconds = max_duration_seconds
        self._chunk_duration_seconds = chunk_duration_seconds

    def normalize(
        self,
        media: BinaryIO,
        *,
        filename: str | None,
        content_type: str | None,
    ) -> NormalizedMedia:
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
                resampler = av.AudioResampler(
                    format="s16",
                    layout="mono",
                    rate=self.sample_rate,
                )
                pcm = bytearray()
                maximum_bytes = int(
                    self._max_duration_seconds * self.sample_rate * self.sample_width
                )
                decoded = (
                    normalized
                    for frame in container.decode(audio_stream)
                    for normalized in resampler.resample(frame)
                )
                for normalized in chain(decoded, resampler.resample(None)):
                    pcm.extend(
                        bytes(normalized.planes[0])[: normalized.samples * self.sample_width]
                    )
                    if len(pcm) > maximum_bytes:
                        raise MediaTooLongError(self._max_duration_seconds)

                sample_count = len(pcm) // self.sample_width
                if not sample_count:
                    raise UnsupportedMediaError
                return NormalizedMedia(
                    chunks=self._chunks(bytes(pcm)),
                    duration_seconds=sample_count / self.sample_rate,
                    is_silent=not any(pcm),
                )
        except NoAudioStreamError:
            raise
        except (av.error.FFmpegError, EOFError, OSError, ValueError) as exc:
            raise UnsupportedMediaError from exc
        finally:
            if not media.closed:
                media.seek(0)

    def _chunks(self, pcm: bytes) -> tuple[NormalizedChunk, ...]:
        chunk_bytes = max(
            self.sample_width,
            int(self._chunk_duration_seconds * self.sample_rate) * self.sample_width,
        )
        chunks: list[NormalizedChunk] = []
        for index, offset in enumerate(range(0, len(pcm), chunk_bytes)):
            chunk_pcm = pcm[offset : offset + chunk_bytes]
            output = io.BytesIO()
            with wave.open(output, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(self.sample_width)
                wav_file.setframerate(self.sample_rate)
                wav_file.writeframes(chunk_pcm)
            output.seek(0)
            start_samples = offset // self.sample_width
            chunks.append(
                NormalizedChunk(
                    index=index,
                    start_seconds=start_samples / self.sample_rate,
                    media=output,
                )
            )
        return tuple(chunks)

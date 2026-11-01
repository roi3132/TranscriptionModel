"""Typed application configuration loaded from environment variables."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    Every field can be overridden with a ``TRANSCRIPTION_`` prefixed environment
    variable. Keeping model and resource limits here makes the application portable
    between a laptop, a CPU container, and a future GPU deployment.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="TRANSCRIPTION_",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    application_name: str = "Transcription Service"
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    api_prefix: str = "/v1"

    model_id: str = "Systran/faster-whisper-small"
    model_revision: str = "536b0662742c02347bc0e980a01041f333bce120"
    device: Literal["auto", "cpu", "cuda"] = "cpu"
    compute_type: str = "int8"
    beam_size: int = Field(default=5, gt=0)
    vad_filter: bool = True

    max_upload_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    max_media_duration_seconds: float = Field(default=15 * 60, gt=0)
    max_concurrent_transcriptions: int = Field(default=1, gt=0)
    inference_queue_timeout_seconds: float = Field(default=5, gt=0)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance."""

    return Settings()

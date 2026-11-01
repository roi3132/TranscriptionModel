"""Configuration tests."""

import pytest
from pydantic import ValidationError

from transcription_service.core.config import Settings


def test_settings_have_cpu_safe_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.model_id == "Systran/faster-whisper-small"
    assert settings.model_revision == "536b0662742c02347bc0e980a01041f333bce120"
    assert settings.device == "cpu"
    assert settings.compute_type == "int8"
    assert settings.max_concurrent_transcriptions == 1
    assert settings.max_media_duration_seconds == 900


def test_settings_read_prefixed_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSCRIPTION_DEVICE", "cuda")
    monkeypatch.setenv("TRANSCRIPTION_COMPUTE_TYPE", "float16")
    monkeypatch.setenv("TRANSCRIPTION_MAX_UPLOAD_BYTES", "4096")

    settings = Settings(_env_file=None)

    assert settings.device == "cuda"
    assert settings.compute_type == "float16"
    assert settings.max_upload_bytes == 4096


def test_settings_reject_non_positive_limits() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_upload_bytes=0)


@pytest.mark.parametrize("prefix", ["v1", "/v1/", "/v1?query=1"])
def test_rejects_api_prefixes_that_cannot_be_mounted(prefix: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_prefix=prefix)

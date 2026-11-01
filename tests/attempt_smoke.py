"""Opt-in real-model smoke test for the production isolated runner."""

import json
import tempfile
import wave
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from transcription_service.adapters.jobs import new_job
from transcription_service.core.config import Settings
from transcription_service.domain import AudioMode, ModelProfile
from transcription_service.services.execution import IsolatedJobRunner


def main() -> None:
    settings = Settings(_env_file=None, job_timeout_seconds=120)
    with tempfile.TemporaryDirectory(prefix="attempt-smoke-") as directory:
        media = Path(directory) / "smoke.media"
        with wave.open(str(media), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes((1000).to_bytes(2, "little", signed=True) * 16000)
        job = new_job(
            job_id=str(uuid4()),
            media_key=media.name,
            filename="smoke.wav",
            content_type="audio/wav",
            language="en",
            audio_mode=AudioMode.SPEECH,
            model_profile=ModelProfile.FASTER,
            model_revision=settings.model_revision,
            media_sha256="smoke",
            cache_key="smoke",
            idempotency_key=None,
            size_bytes=media.stat().st_size,
            max_attempts=1,
        )
        for mode in (AudioMode.SPEECH, AudioMode.MUSIC):
            media.with_suffix(".chunks.json").unlink(missing_ok=True)
            outcome = IsolatedJobRunner(120, settings=settings).run(
                replace(job, audio_mode=mode), media, lambda: None
            )
            assert outcome.result is not None, outcome
            assert outcome.result["duration_seconds"] == 1.0
            print(
                json.dumps({"status": "succeeded", "mode": mode, "model": outcome.result["model"]})
            )


if __name__ == "__main__":
    main()

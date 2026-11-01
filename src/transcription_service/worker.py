"""Independent durable transcription worker entry point."""

import structlog

from transcription_service.adapters.faster_whisper import FasterWhisperTranscriber
from transcription_service.adapters.jobs import (
    LocalMediaStore,
    PostgresJobRepository,
    RedisJobBroker,
)
from transcription_service.adapters.media import PyAvMediaInspector
from transcription_service.core.config import get_settings
from transcription_service.core.logging import configure_logging
from transcription_service.services.jobs import TranscriptionJobWorker
from transcription_service.services.transcription import TranscriptionService

logger = structlog.get_logger(__name__)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    repository = PostgresJobRepository(settings.database_url)
    broker = RedisJobBroker(settings.redis_url, settings.redis_queue_name)
    media_store = LocalMediaStore(settings.media_storage_path)
    repository.migrate()
    repository.ping()
    broker.ping()

    transcriber = FasterWhisperTranscriber.from_settings(settings)
    service = TranscriptionService(
        transcriber,
        PyAvMediaInspector(max_duration_seconds=settings.max_media_duration_seconds),
        settings,
        transcriber_factory=FasterWhisperTranscriber.from_settings,
    )
    logger.info("worker_ready", model=transcriber.model_id)
    try:
        TranscriptionJobWorker(repository, broker, media_store, service).run_forever()
    finally:
        broker.close()


if __name__ == "__main__":
    main()

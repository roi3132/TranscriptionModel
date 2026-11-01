"""Independent durable transcription worker supervisor entry point."""

import structlog

from transcription_service.adapters.jobs import (
    LocalMediaStore,
    PostgresJobRepository,
    RedisJobBroker,
)
from transcription_service.core.config import get_settings
from transcription_service.core.logging import configure_logging
from transcription_service.services.execution import IsolatedJobRunner
from transcription_service.services.jobs import TranscriptionJobWorker

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

    runner = IsolatedJobRunner(settings.job_timeout_seconds)
    logger.info("worker_ready", attempt_timeout_seconds=settings.job_timeout_seconds)
    try:
        TranscriptionJobWorker(
            repository,
            broker,
            media_store,
            runner,
            settings,
        ).run_forever()
    finally:
        broker.close()


if __name__ == "__main__":
    main()

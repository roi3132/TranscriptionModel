"""The two configured models and their thread-safe lazy loading policy."""

from threading import Lock

import structlog

from transcription_service.core.config import Settings
from transcription_service.core.errors import ModelNotReadyError
from transcription_service.domain import ModelProfile, Transcriber, TranscriberFactory

logger = structlog.get_logger(__name__)


class ModelRegistry:
    """Keep the startup model warm and initialize the optional larger model once."""

    def __init__(
        self,
        default: Transcriber,
        settings: Settings,
        factory: TranscriberFactory | None,
    ) -> None:
        self._default = default
        self._larger: Transcriber | None = None
        self._larger_settings = settings.model_copy(
            update={
                "model_id": settings.larger_model_id,
                "model_revision": settings.larger_model_revision,
            }
        )
        self._factory = factory
        self._load_lock = Lock()

    def get(self, profile: ModelProfile) -> Transcriber:
        """Resolve a model in a worker thread; failed loads remain retryable."""

        if profile is ModelProfile.FASTER:
            return self._default

        with self._load_lock:
            if self._larger is None:
                if self._factory is None:
                    raise ModelNotReadyError
                logger.info("larger_model_loading", model=self._larger_settings.model_id)
                self._larger = self._factory(self._larger_settings)
                logger.info("larger_model_ready", model=self._larger.model_id)
            return self._larger

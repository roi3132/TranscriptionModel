"""Mutable process health state owned by the application lifespan."""

from dataclasses import dataclass


@dataclass(slots=True)
class RuntimeStatus:
    """Tracks whether this process has a usable transcription model."""

    model_ready: bool = False
    model_error: str | None = None

    def mark_ready(self) -> None:
        """Mark the model as available for inference."""

        self.model_ready = True
        self.model_error = None

    def mark_unavailable(self, reason: str | None = None) -> None:
        """Mark the model as unavailable without exposing the reason to clients."""

        self.model_ready = False
        self.model_error = reason

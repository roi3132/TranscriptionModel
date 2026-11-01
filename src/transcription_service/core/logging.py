"""Structured logging configuration and request context helpers."""

import logging
import sys

import structlog


def configure_logging(log_level: str) -> None:
    """Configure stdlib logging and structlog to emit one JSON object per event."""

    numeric_level = logging.getLevelNamesMapping()[log_level]
    logging.basicConfig(
        format="%(message)s",
        level=numeric_level,
        stream=sys.stdout,
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

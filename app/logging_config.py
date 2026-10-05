"""Structured JSON logging.

Logs are emitted as single-line JSON objects. Correlation fields
(``payment_id``, ``event_id``, ``attempt_no``) are attached through
:func:`get_logger`. The configured API key and the full contents of arbitrary
payment metadata are never logged by the application.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

CORRELATION_FIELDS = ("payment_id", "event_id", "attempt_no", "event_type", "service")
_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON with correlation fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in CORRELATION_FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = str(value)
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key not in payload and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class BoundLogger:
    """A lightweight logger that carries structured correlation fields."""

    __slots__ = ("_logger", "_fields")

    def __init__(self, logger: logging.Logger, fields: dict[str, Any] | None = None) -> None:
        self._logger = logger
        self._fields = dict(fields or {})

    def bind(self, **fields: Any) -> BoundLogger:
        """Return a new logger with additional correlation fields."""
        merged = {**self._fields, **fields}
        return BoundLogger(self._logger, merged)

    def _log(self, level: int, message: str, *args: Any, **kwargs: Any) -> None:
        if args or kwargs:
            message = message % args if args else message.format(**kwargs)
        extra = {**self._fields}
        self._logger.log(level, message, extra=extra)

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log at DEBUG level."""
        if self._logger.isEnabledFor(logging.DEBUG):
            self._log(logging.DEBUG, message, *args, **kwargs)

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log at INFO level."""
        if self._logger.isEnabledFor(logging.INFO):
            self._log(logging.INFO, message, *args, **kwargs)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log at WARNING level."""
        if self._logger.isEnabledFor(logging.WARNING):
            self._log(logging.WARNING, message, *args, **kwargs)

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log at ERROR level."""
        if self._logger.isEnabledFor(logging.ERROR):
            self._log(logging.ERROR, message, *args, **kwargs)

    def exception(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log at ERROR level including the active exception traceback."""
        if args or kwargs:
            message = message % args if args else message.format(**kwargs)
        extra = {**self._fields}
        self._logger.exception(message, extra=extra)


def configure_logging(level: str = "INFO", *, service: str = "payment-service") -> None:
    """Install the JSON formatter on the root logger (idempotent)."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
    for noisy in ("uvicorn.access", "aiormq", "aio_pika"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger(service)


def get_logger(name: str, **fields: Any) -> BoundLogger:
    """Return a :class:`BoundLogger` for ``name`` with optional fields."""
    return BoundLogger(logging.getLogger(name), fields)

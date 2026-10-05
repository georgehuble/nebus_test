"""Environment-based configuration.

All configuration is supplied through environment variables (optionally via a
``.env`` file). Missing or empty required values make the application fail fast
at startup instead of silently running with insecure defaults.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_WEBHOOK_NAMESPACE = "6f9619ff-8b86-d011-b42d-00cf4fc964ff"


class Settings(BaseSettings):
    """Runtime settings loaded from the process environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Required ---------------------------------------------------------
    api_key: SecretStr = Field(min_length=1)
    database_url: str = Field(min_length=1)
    rabbitmq_url: str = Field(min_length=1)

    # --- Processing -------------------------------------------------------
    max_attempts: int = Field(default=3, ge=1)
    max_recoveries: int = Field(default=5, ge=0)
    retry_base_delay_seconds: float = Field(default=1.0, gt=0)
    gateway_min_delay_seconds: float = Field(default=2.0, ge=0)
    gateway_max_delay_seconds: float = Field(default=5.0, ge=0)
    gateway_decline_probability: float = Field(default=0.10, ge=0, le=1)

    # --- Leases and heartbeats -------------------------------------------
    attempt_lease_seconds: float = Field(default=15.0, gt=0)
    heartbeat_interval_seconds: float = Field(default=3.0, gt=0)
    relay_claim_lease_seconds: float = Field(default=60.0, gt=0)

    # --- Background loops -------------------------------------------------
    relay_batch_size: int = Field(default=50, ge=1)
    relay_poll_interval_seconds: float = Field(default=1.0, gt=0)
    retry_poll_interval_seconds: float = Field(default=0.5, gt=0)

    # --- Network ----------------------------------------------------------
    webhook_timeout_seconds: float = Field(default=10.0, gt=0)

    # --- Observability ----------------------------------------------------
    log_level: str = Field(default="INFO")
    service_name: str = Field(default="payment-service")

    @field_validator("database_url")
    @classmethod
    def _require_asyncpg_url(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            msg = "DATABASE_URL must use the 'postgresql+asyncpg://' scheme"
            raise ValueError(msg)
        return value

    @field_validator("rabbitmq_url")
    @classmethod
    def _require_amqp_url(cls, value: str) -> str:
        if not value.startswith("amqp://"):
            msg = "RABBITMQ_URL must use the 'amqp://' scheme"
            raise ValueError(msg)
        return value

    @field_validator("gateway_min_delay_seconds")
    @classmethod
    def _validate_delay_bounds(cls, value: float) -> float:
        if value > 5.0:
            msg = "gateway_min_delay_seconds must be within the [2, 5] second emulation window"
            raise ValueError(msg)
        return value

    def retry_delay_seconds(self, attempt_no: int) -> float:
        """Exponentially increasing delay before the attempt ``attempt_no + 1``."""
        return float(self.retry_base_delay_seconds * (2 ** max(attempt_no - 1, 0)))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()

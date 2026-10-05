"""Shared infrastructure constants and helpers for integration/E2E tests."""

from __future__ import annotations

import os
from pathlib import Path

from alembic.config import Config as AlembicConfig

REPO_ROOT = Path(__file__).resolve().parents[1]

TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://payments:payments@localhost:15432/payments",
)
TEST_RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost:15673/")
TEST_API_KEY = os.environ.get("API_KEY", "dev-api-key-change-me")


def alembic_config() -> AlembicConfig:
    """Return an Alembic config pointing at the repository migrations."""
    return AlembicConfig(str(REPO_ROOT / "alembic.ini"))

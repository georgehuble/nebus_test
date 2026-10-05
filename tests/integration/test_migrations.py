"""Reproducible migrations against a real PostgreSQL instance."""

from __future__ import annotations

import os

import pytest
from alembic import command
from app.infrastructure.db import Database
from sqlalchemy import text

from tests.infra import TEST_DATABASE_URL, alembic_config

pytestmark = pytest.mark.integration


async def test_schema_contains_expected_objects(database: Database) -> None:
    async with database.session() as session:
        tables = set(
            (await session.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).scalars()
        )
        assert {"payments", "outbox"} <= tables
        indexes = set(
            (await session.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"))).scalars()
        )
        assert {
            "ix_outbox_unpublished",
            "ix_outbox_retry_due",
            "ix_payments_status_pending",
        } <= indexes
        checks = set(
            (
                await session.execute(
                    text(
                        "SELECT conname FROM pg_constraint WHERE contype = 'c' "
                        "AND conrelid IN ('payments'::regclass, 'outbox'::regclass)"
                    )
                )
            ).scalars()
        )
        assert {"ck_payments_amount_range", "ck_outbox_attempt_state"} <= checks


def test_migrations_roundtrip_on_empty_database() -> None:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    config = alembic_config()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    command.upgrade(config, "head")  # idempotent second application

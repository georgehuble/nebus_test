"""SQLAlchemy mappings for the ``payments`` and ``outbox`` tables."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class Payment(Base):
    """A payment request and its durable webhook delivery state."""

    __tablename__ = "payments"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    request_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    # SQL column is ``metadata`` but the Python attribute must be ``metadata_``
    # because ``metadata`` is reserved on declarative classes.
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    webhook_url: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Delivery state, deliberately separate from the business ``status``.
    webhook_event_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    webhook_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'"), default="pending"
    )
    webhook_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    webhook_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint("amount > 0 AND amount <= 999999999.99", name="ck_payments_amount_range"),
        CheckConstraint("currency IN ('RUB','USD','EUR')", name="ck_payments_currency"),
        CheckConstraint("char_length(description) <= 500", name="ck_payments_description_length"),
        CheckConstraint("status IN ('pending','succeeded','failed')", name="ck_payments_status"),
        CheckConstraint(
            "webhook_status IN ('pending','delivered','failed')",
            name="ck_payments_webhook_status",
        ),
        CheckConstraint("webhook_attempts >= 0", name="ck_payments_webhook_attempts"),
        Index("ix_payments_status_pending", "status", postgresql_where=text("status = 'pending'")),
        Index("ix_payments_created_at", "created_at"),
    )


class OutboxEvent(Base):
    """A durable outbox record with separate publication and attempt accounting."""

    __tablename__ = "outbox"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    payment_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("now()"))

    # Publication accounting (relay only).
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Consumer processing-attempt accounting.
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    next_attempt_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt_state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'idle'"), default="idle")
    attempt_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_epoch: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    attempt_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recoveries: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("payment_id", "event_type", name="uq_outbox_payment_event"),
        CheckConstraint(
            "attempt_state IN ('idle','open','awaiting_retry','completed')",
            name="ck_outbox_attempt_state",
        ),
        CheckConstraint("attempt_no >= 0", name="ck_outbox_attempt_no"),
        CheckConstraint("attempt_epoch >= 0", name="ck_outbox_attempt_epoch"),
        Index(
            "ix_outbox_unpublished",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
        ),
        Index("ix_outbox_created_at", "created_at"),
        Index(
            "ix_outbox_retry_due",
            "next_attempt_at",
            postgresql_where=text("attempt_state = 'awaiting_retry'"),
        ),
    )

"""Repository layer with short transactions and per-attempt fencing.

Publication accounting (``claimed_at``/``published_at``) and consumer
processing-attempt accounting (``attempt_no`` and friends) live on the same
``outbox`` row but are never mixed: relay methods touch only publication fields
while consumer methods touch only attempt fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, case, exists, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.dto import PaymentCreateRequest
from app.domain.events import PAYMENT_CREATED_EVENT, PaymentCreatedEvent
from app.domain.models import AttemptState, PaymentStatus, WebhookStatus
from app.infrastructure.db import Database
from app.infrastructure.models import OutboxEvent, Payment
from app.infrastructure.system import SystemClock
from app.protocols import Clock

_dummy_clock = SystemClock()


@dataclass(frozen=True, slots=True)
class PaymentRecord:
    """Detached snapshot of a payment row."""

    id: UUID
    idempotency_key: str
    request_fingerprint: str
    amount: Decimal
    currency: str
    description: str
    metadata: dict[str, Any]
    status: PaymentStatus
    webhook_url: str
    created_at: datetime
    processed_at: datetime | None
    webhook_event_id: UUID | None
    webhook_status: WebhookStatus
    webhook_attempts: int
    webhook_last_error: str | None


@dataclass(frozen=True, slots=True)
class CreateResult:
    """Outcome of an idempotent payment creation."""

    created: bool
    payment: PaymentRecord


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    """Detached snapshot of an outbox row."""

    id: UUID
    payment_id: UUID
    event_type: str
    payload: dict[str, Any]
    created_at: datetime
    attempt_no: int
    next_attempt_no: int | None
    attempt_state: AttemptState
    attempt_epoch: int
    attempt_owner: str | None
    attempt_lease_expires_at: datetime | None
    next_attempt_at: datetime | None
    recoveries: int
    last_error: str | None
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class AttemptClaim:
    """A successfully claimed (or taken over) processing attempt."""

    attempt_no: int
    attempt_epoch: int


def _to_payment(model: Payment) -> PaymentRecord:
    return PaymentRecord(
        id=model.id,
        idempotency_key=model.idempotency_key,
        request_fingerprint=model.request_fingerprint,
        amount=model.amount,
        currency=model.currency,
        description=model.description,
        metadata=model.metadata_,
        status=PaymentStatus(model.status),
        webhook_url=model.webhook_url,
        created_at=model.created_at,
        processed_at=model.processed_at,
        webhook_event_id=model.webhook_event_id,
        webhook_status=WebhookStatus(model.webhook_status),
        webhook_attempts=model.webhook_attempts,
        webhook_last_error=model.webhook_last_error,
    )


def _to_outbox(model: OutboxEvent) -> OutboxRecord:
    return OutboxRecord(
        id=model.id,
        payment_id=model.payment_id,
        event_type=model.event_type,
        payload=model.payload,
        created_at=model.created_at,
        attempt_no=model.attempt_no,
        next_attempt_no=model.next_attempt_no,
        attempt_state=AttemptState(model.attempt_state),
        attempt_epoch=model.attempt_epoch,
        attempt_owner=model.attempt_owner,
        attempt_lease_expires_at=model.attempt_lease_expires_at,
        next_attempt_at=model.next_attempt_at,
        recoveries=model.recoveries,
        last_error=model.last_error,
        published_at=model.published_at,
    )


def _fencing_clause(payment_id: UUID, owner: str, epoch: int) -> Any:
    """EXISTS clause that guards a payment write with the attempt owner/epoch."""
    return exists(
        select(OutboxEvent.id).where(
            OutboxEvent.payment_id == payment_id,
            OutboxEvent.attempt_owner == owner,
            OutboxEvent.attempt_epoch == epoch,
        )
    )


class PaymentRepository:
    """Reads and writes payments, plus the atomic Payment+Outbox creation."""

    def __init__(self, database: Database, *, clock: Clock | None = None) -> None:
        self._db = database
        self._clock = clock or _dummy_clock

    async def create(
        self,
        *,
        idempotency_key: str,
        request: PaymentCreateRequest,
        fingerprint: str,
        occurred_at: datetime | None = None,
    ) -> CreateResult:
        """Insert a pending payment and its initial outbox event atomically."""
        created_at = occurred_at or self._clock.now()
        async with self._db.session() as session, session.begin():
            insert_stmt = (
                pg_insert(Payment)
                .values(
                    idempotency_key=idempotency_key,
                    request_fingerprint=fingerprint,
                    amount=request.amount,
                    currency=request.currency,
                    description=request.description,
                    metadata_=request.metadata,
                    status=PaymentStatus.PENDING.value,
                    webhook_url=str(request.webhook_url),
                    created_at=created_at,
                    webhook_status=WebhookStatus.PENDING.value,
                    webhook_attempts=0,
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(Payment.id)
            )
            result = await session.execute(insert_stmt)
            row = result.first()
            if row is not None:
                payment_id: UUID = row[0]
                # The outbox row id doubles as the message ``event_id`` so a
                # delivered message can always be mapped back to its outbox row.
                event_id = uuid4()
                event = PaymentCreatedEvent(
                    event_id=event_id,
                    payment_id=payment_id,
                    occurred_at=created_at,
                )
                await session.execute(
                    pg_insert(OutboxEvent).values(
                        id=event_id,
                        payment_id=payment_id,
                        event_type=PAYMENT_CREATED_EVENT,
                        payload=event.model_dump(mode="json"),
                        created_at=created_at,
                    )
                )
                model = await session.get(Payment, payment_id)
                assert model is not None  # noqa: S101 - just inserted in this transaction
                return CreateResult(created=True, payment=_to_payment(model))
            existing = await self._get_by_key(session, idempotency_key)
            if existing is None:  # pragma: no cover - conflict implies a committed row
                msg = "idempotency conflict without a stored payment"
                raise RuntimeError(msg)
            return CreateResult(created=False, payment=existing)

    async def _get_by_key(self, session: AsyncSession, idempotency_key: str) -> PaymentRecord | None:
        result = await session.execute(select(Payment).where(Payment.idempotency_key == idempotency_key))
        model = result.scalar_one_or_none()
        return _to_payment(model) if model is not None else None

    async def get_by_id(self, payment_id: UUID) -> PaymentRecord | None:
        """Return a payment snapshot or ``None``."""
        async with self._db.session() as session:
            model = await session.get(Payment, payment_id)
            return _to_payment(model) if model is not None else None

    async def mark_terminal(
        self,
        *,
        payment_id: UUID,
        status: PaymentStatus,
        processed_at: datetime,
        webhook_event_id: UUID,
        owner: str,
        epoch: int,
    ) -> bool:
        """Single conditional update that wins the terminal transition."""
        async with self._db.session() as session, session.begin():
            stmt = (
                update(Payment)
                .where(
                    Payment.id == payment_id,
                    Payment.status == PaymentStatus.PENDING.value,
                    _fencing_clause(payment_id, owner, epoch),
                )
                .values(
                    status=status.value,
                    processed_at=processed_at,
                    webhook_event_id=webhook_event_id,
                )
                .returning(Payment.id)
            )
            result = await session.execute(stmt)
            return result.first() is not None

    async def record_delivery_success(
        self,
        *,
        payment_id: UUID,
        event_id: UUID,
        owner: str,
        epoch: int,
    ) -> bool:
        """Persist a successful webhook delivery (fenced by owner/epoch)."""
        async with self._db.session() as session, session.begin():
            stmt = (
                update(Payment)
                .where(Payment.id == payment_id, _fencing_clause(payment_id, owner, epoch))
                .values(
                    webhook_status=WebhookStatus.DELIVERED.value,
                    webhook_attempts=Payment.webhook_attempts + 1,
                    webhook_last_error=None,
                    webhook_event_id=event_id,
                )
                .returning(Payment.id)
            )
            result = await session.execute(stmt)
            return result.first() is not None

    async def record_delivery_failure(
        self,
        *,
        payment_id: UUID,
        error: str,
        owner: str,
        epoch: int,
        final: bool,
    ) -> bool:
        """Persist a failed webhook delivery attempt (fenced by owner/epoch)."""
        status = WebhookStatus.FAILED.value if final else WebhookStatus.PENDING.value
        async with self._db.session() as session, session.begin():
            stmt = (
                update(Payment)
                .where(Payment.id == payment_id, _fencing_clause(payment_id, owner, epoch))
                .values(
                    webhook_status=status,
                    webhook_attempts=Payment.webhook_attempts + 1,
                    webhook_last_error=error,
                )
                .returning(Payment.id)
            )
            result = await session.execute(stmt)
            return result.first() is not None


class OutboxRepository:
    """Relay claim/mark and consumer attempt accounting for the outbox table."""

    def __init__(self, database: Database, *, clock: Clock | None = None) -> None:
        self._db = database
        self._clock = clock or _dummy_clock

    async def fetch(self, event_id: UUID) -> OutboxRecord | None:
        """Return an outbox snapshot or ``None``."""
        async with self._db.session() as session:
            model = await session.get(OutboxEvent, event_id)
            return _to_outbox(model) if model is not None else None

    async def fetch_by_payment(self, payment_id: UUID) -> OutboxRecord | None:
        """Return the outbox snapshot belonging to a payment."""
        async with self._db.session() as session:
            result = await session.execute(select(OutboxEvent).where(OutboxEvent.payment_id == payment_id))
            model = result.scalar_one_or_none()
            return _to_outbox(model) if model is not None else None

    # --- Relay ------------------------------------------------------------

    async def claim_batch(self, *, batch_size: int, lease_seconds: float) -> list[OutboxRecord]:
        """Claim a batch of unpublished records for publication."""
        claimed_at = self._clock.now()
        stale_before = claimed_at - timedelta(seconds=lease_seconds)
        async with self._db.session() as session, session.begin():
            select_stmt = (
                select(OutboxEvent)
                .where(
                    OutboxEvent.published_at.is_(None),
                    or_(
                        OutboxEvent.claimed_at.is_(None),
                        OutboxEvent.claimed_at < stale_before,
                    ),
                )
                .order_by(OutboxEvent.created_at)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            models = (await session.execute(select_stmt)).scalars().all()
            if not models:
                return []
            ids = [model.id for model in models]
            await session.execute(update(OutboxEvent).where(OutboxEvent.id.in_(ids)).values(claimed_at=claimed_at))
            for model in models:
                model.claimed_at = claimed_at
            return [_to_outbox(model) for model in models]

    async def mark_published(self, event_ids: list[UUID]) -> None:
        """Retain records in a published state after confirmed publication."""
        if not event_ids:
            return
        async with self._db.session() as session, session.begin():
            await session.execute(
                update(OutboxEvent).where(OutboxEvent.id.in_(event_ids)).values(published_at=self._clock.now())
            )

    # --- Consumer attempts ------------------------------------------------

    async def claim_attempt(
        self,
        *,
        event_id: UUID,
        message_attempt_no: int,
        owner: str,
        lease_seconds: float,
    ) -> AttemptClaim | None:
        """Atomically start a new attempt (increment) or a due scheduled retry."""
        now = self._clock.now()
        expires = now + timedelta(seconds=lease_seconds)
        new_attempt_no = case(
            (OutboxEvent.attempt_state == AttemptState.IDLE.value, 1),
            else_=OutboxEvent.next_attempt_no,
        )
        eligible = or_(
            and_(
                OutboxEvent.attempt_state == AttemptState.IDLE.value,
                literal(message_attempt_no) == 1,
            ),
            and_(
                OutboxEvent.attempt_state == AttemptState.AWAITING_RETRY.value,
                literal(message_attempt_no) == OutboxEvent.next_attempt_no,
                or_(
                    OutboxEvent.next_attempt_at.is_(None),
                    OutboxEvent.next_attempt_at <= now,
                ),
            ),
        )
        async with self._db.session() as session, session.begin():
            stmt = (
                update(OutboxEvent)
                .where(OutboxEvent.id == event_id, eligible)
                .values(
                    attempt_no=new_attempt_no,
                    attempt_state=AttemptState.OPEN.value,
                    attempt_epoch=OutboxEvent.attempt_epoch + 1,
                    attempt_owner=owner,
                    attempt_lease_expires_at=expires,
                    attempt_heartbeat_at=now,
                    recoveries=0,
                    last_attempt_at=now,
                    next_attempt_at=None,
                    last_error=None,
                )
                .returning(OutboxEvent.attempt_no, OutboxEvent.attempt_epoch)
            )
            row = (await session.execute(stmt)).first()
            if row is None:
                return None
            return AttemptClaim(attempt_no=row[0], attempt_epoch=row[1])

    async def takeover(
        self,
        *,
        event_id: UUID,
        owner: str,
        lease_seconds: float,
        max_recoveries: int,
    ) -> AttemptClaim | None:
        """Recover an attempt whose lease expired, fencing the previous owner."""
        now = self._clock.now()
        expires = now + timedelta(seconds=lease_seconds)
        async with self._db.session() as session, session.begin():
            stmt = (
                update(OutboxEvent)
                .where(
                    OutboxEvent.id == event_id,
                    OutboxEvent.attempt_state == AttemptState.OPEN.value,
                    OutboxEvent.attempt_lease_expires_at.is_not(None),
                    OutboxEvent.attempt_lease_expires_at < now,
                    OutboxEvent.recoveries < max_recoveries,
                )
                .values(
                    attempt_epoch=OutboxEvent.attempt_epoch + 1,
                    attempt_owner=owner,
                    attempt_lease_expires_at=expires,
                    attempt_heartbeat_at=now,
                    recoveries=OutboxEvent.recoveries + 1,
                )
                .returning(OutboxEvent.attempt_no, OutboxEvent.attempt_epoch)
            )
            row = (await session.execute(stmt)).first()
            if row is None:
                return None
            return AttemptClaim(attempt_no=row[0], attempt_epoch=row[1])

    async def heartbeat(
        self,
        *,
        event_id: UUID,
        owner: str,
        epoch: int,
        lease_seconds: float,
    ) -> bool:
        """Renew the lease while this owner still holds the claim."""
        now = self._clock.now()
        expires = now + timedelta(seconds=lease_seconds)
        async with self._db.session() as session, session.begin():
            stmt = (
                update(OutboxEvent)
                .where(
                    OutboxEvent.id == event_id,
                    OutboxEvent.attempt_owner == owner,
                    OutboxEvent.attempt_epoch == epoch,
                    OutboxEvent.attempt_state == AttemptState.OPEN.value,
                )
                .values(attempt_lease_expires_at=expires, attempt_heartbeat_at=now)
                .returning(OutboxEvent.id)
            )
            return (await session.execute(stmt)).first() is not None

    async def schedule_retry(
        self,
        *,
        event_id: UUID,
        owner: str,
        epoch: int,
        next_attempt_no: int,
        next_attempt_at: datetime,
        error: str,
    ) -> bool:
        """Durably schedule the next attempt and release the claim."""
        async with self._db.session() as session, session.begin():
            stmt = (
                update(OutboxEvent)
                .where(
                    OutboxEvent.id == event_id,
                    OutboxEvent.attempt_owner == owner,
                    OutboxEvent.attempt_epoch == epoch,
                )
                .values(
                    attempt_state=AttemptState.AWAITING_RETRY.value,
                    next_attempt_no=next_attempt_no,
                    next_attempt_at=next_attempt_at,
                    last_error=error,
                    attempt_owner=None,
                    attempt_lease_expires_at=None,
                    attempt_heartbeat_at=None,
                )
                .returning(OutboxEvent.id)
            )
            return (await session.execute(stmt)).first() is not None

    async def complete(self, *, event_id: UUID, owner: str, epoch: int, error: str | None = None) -> bool:
        """Close the attempt after its work is durably finished."""
        async with self._db.session() as session, session.begin():
            stmt = (
                update(OutboxEvent)
                .where(
                    OutboxEvent.id == event_id,
                    OutboxEvent.attempt_owner == owner,
                    OutboxEvent.attempt_epoch == epoch,
                )
                .values(
                    attempt_state=AttemptState.COMPLETED.value,
                    attempt_owner=None,
                    attempt_lease_expires_at=None,
                    attempt_heartbeat_at=None,
                    next_attempt_at=None,
                    last_error=error,
                )
                .returning(OutboxEvent.id)
            )
            return (await session.execute(stmt)).first() is not None

    async def fetch_due_retries(self, *, batch_size: int) -> list[OutboxRecord]:
        """Select retries whose scheduled time has arrived."""
        now = self._clock.now()
        async with self._db.session() as session, session.begin():
            stmt = (
                select(OutboxEvent)
                .where(
                    OutboxEvent.attempt_state == AttemptState.AWAITING_RETRY.value,
                    OutboxEvent.next_attempt_at.is_not(None),
                    OutboxEvent.next_attempt_at <= now,
                )
                .order_by(OutboxEvent.next_attempt_at)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            models = (await session.execute(stmt)).scalars().all()
            return [_to_outbox(model) for model in models]

    async def mark_dispatched(self, event_ids: list[UUID]) -> None:
        """Clear the retry schedule after a confirmed dispatcher publication."""
        if not event_ids:
            return
        async with self._db.session() as session, session.begin():
            await session.execute(
                update(OutboxEvent)
                .where(
                    OutboxEvent.id.in_(event_ids),
                    OutboxEvent.attempt_state == AttemptState.AWAITING_RETRY.value,
                )
                .values(next_attempt_at=None)
            )

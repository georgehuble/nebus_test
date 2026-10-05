"""Composition root wiring settings, repositories, gateway, broker and tasks."""

from __future__ import annotations

from dataclasses import dataclass

from app.application.payments import PaymentService
from app.application.processing import PaymentProcessor
from app.gateway.emulator import EmulatedPaymentGateway
from app.infrastructure.db import Database
from app.infrastructure.messaging import Broker, BrokerPublisher
from app.infrastructure.relay import OutboxRelay
from app.infrastructure.repositories import OutboxRepository, PaymentRepository
from app.infrastructure.retry_dispatcher import RetryDispatcher
from app.infrastructure.system import AsyncioSleeper, DefaultRandomSource, SystemClock
from app.infrastructure.webhook_client import HttpxWebhookSender
from app.settings import Settings


@dataclass(frozen=True, slots=True)
class ApiContainer:
    """Dependencies used by the HTTP API process."""

    settings: Settings
    database: Database
    payments: PaymentRepository
    outbox: OutboxRepository
    service: PaymentService


@dataclass(frozen=True, slots=True)
class ConsumerContainer:
    """Dependencies used by the consumer process (handler, relay and dispatcher)."""

    settings: Settings
    database: Database
    payments: PaymentRepository
    outbox: OutboxRepository
    gateway: EmulatedPaymentGateway
    webhook_sender: HttpxWebhookSender
    broker: Broker
    processor: PaymentProcessor
    relay: OutboxRelay
    retry_dispatcher: RetryDispatcher


def build_api_container(settings: Settings) -> ApiContainer:
    """Wire the HTTP API dependencies."""
    database = Database(settings.database_url)
    clock = SystemClock()
    payments = PaymentRepository(database, clock=clock)
    outbox = OutboxRepository(database, clock=clock)
    return ApiContainer(
        settings=settings,
        database=database,
        payments=payments,
        outbox=outbox,
        service=PaymentService(payments),
    )


def build_consumer_container(settings: Settings) -> ConsumerContainer:
    """Wire the consumer dependencies including broker, relay and dispatcher."""
    database = Database(settings.database_url)
    clock = SystemClock()
    payments = PaymentRepository(database, clock=clock)
    outbox = OutboxRepository(database, clock=clock)
    gateway = EmulatedPaymentGateway(
        random_source=DefaultRandomSource(),
        sleeper=AsyncioSleeper(),
        min_delay_seconds=settings.gateway_min_delay_seconds,
        max_delay_seconds=settings.gateway_max_delay_seconds,
        decline_probability=settings.gateway_decline_probability,
    )
    webhook_sender = HttpxWebhookSender.create(timeout_seconds=settings.webhook_timeout_seconds)
    broker = Broker(settings.rabbitmq_url, prefetch_count=1)
    publisher = BrokerPublisher(broker)
    processor = PaymentProcessor(
        settings=settings,
        payments=payments,
        outbox=outbox,
        gateway=gateway,
        webhooks=webhook_sender,
        publisher=publisher,
        clock=clock,
    )
    relay = OutboxRelay(outbox=outbox, publisher=publisher, settings=settings)
    retry_dispatcher = RetryDispatcher(outbox=outbox, publisher=publisher, settings=settings)
    return ConsumerContainer(
        settings=settings,
        database=database,
        payments=payments,
        outbox=outbox,
        gateway=gateway,
        webhook_sender=webhook_sender,
        broker=broker,
        processor=processor,
        relay=relay,
        retry_dispatcher=retry_dispatcher,
    )

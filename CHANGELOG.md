# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-05

### Added

- Asynchronous payment processing service built with FastAPI, Pydantic v2 and SQLAlchemy 2.0 (async).
- Transactional Outbox pattern with a background relay and publisher confirms.
- Idempotent payment creation keyed by `Idempotency-Key`, with request-fingerprint conflict detection.
- Bounded exponential retries persisted in PostgreSQL and a dead-letter queue for exhausted work.
- At-least-once webhook delivery with a deterministic, stable `event_id`.
- Deterministic external payment gateway emulator behind a narrow protocol.
- Docker Compose development stack (PostgreSQL, RabbitMQ, API, consumer) and an Alembic migration.
- Unit, integration and end-to-end test suites.
- GitHub Actions CI pipeline (lint, strict typing, tests, container build) and image publishing to GHCR.

[Unreleased]: https://github.com/georgehuble/nebus_test/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/georgehuble/nebus_test/releases/tag/v0.1.0

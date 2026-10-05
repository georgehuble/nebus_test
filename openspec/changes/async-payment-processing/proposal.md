## Why

Нужен воспроизводимый микросервис процессинга платежей, который принимает запросы на оплату, асинхронно
обрабатывает их через эмулятор внешнего платёжного шлюза и уведомляет клиента по webhook. Задание проверяет
архитектуру, корректность Outbox pattern, топологию RabbitMQ (exchanges/queues/DLQ), идемпотентность,
обработку ошибок и retry, а также работоспособность Docker-окружения. Это greenfield-проект: в репозитории
есть только каркас OpenSpec, кода и зависимостей нет.

## What Changes

- Новый асинхронный сервис на FastAPI + Pydantic v2 + SQLAlchemy 2.0 (async) + PostgreSQL + RabbitMQ/FastStream +
  Alembic, запускаемый через Docker Compose.
- Два публичных HTTP-эндпоинта: `POST /api/v1/payments` (создание, заголовок `Idempotency-Key`, ответ `202 Accepted`)
  и `GET /api/v1/payments/{payment_id}` (детали платежа). Все эндпоинты защищены статическим `X-API-Key`.
- Сущность `Payment` с суммой `Decimal`, валютой RUB/USD/EUR, описанием, JSON-метаданными, статусом
  pending/succeeded/failed, уникальным idempotency key, webhook URL и UTC-таймстампами создания/обработки.
- Транзакционная запись Payment (pending) и исходного события Outbox в одной DB-транзакции; ответ выдаётся
  после commit, поэтому отказ RabbitMQ не теряет принятый платёж.
- Relay Outbox как фоновая задача внутри процесса consumer: публикация committed-событий в durable RabbitMQ-топологию,
  отметка публикации только после publisher confirm с проверкой маршрутизации; at-least-once с идемпотентной обработкой.
- Один consumer-обработчик, оркестрирующий эмуляцию шлюза (2–5 секунд, 90% успех / 10% бизнес-отказ), обновление
  статуса, устойчивую доставку webhook и повторные попытки.
- Retry: максимум 3 попытки обработки сообщения (включая первую) с экспоненциальной задержкой между ними;
  исчерпание попыток направляет сообщение в Dead Letter Queue.
- Webhook-доставка: HTTP POST с JSON, стабильный `event_id` для дедупликации получателем, таймаут,
  успех = 2xx; at-least-once с явно описанным окном повторной доставки.
- Alembic-миграции для таблиц `payments` и `outbox` (без `create_all`); `.env.example`; README с запуском,
  curl-примерами и ограничениями гарантий.
- Тесты: unit (деньги/валюты/даты, переходы статусов, эмулятор шлюза, retry, классификация ошибок) и
  integration/E2E на реальных PostgreSQL и RabbitMQ, включая конкурентные POST, повторную публикацию и DLQ.

### Non-goals

- Реальные списания денег, интеграция с настоящим платёжным шлюзом.
- Фронтенд, аутентификация пользователей, JWT, роли и мультитенантность.
- Kubernetes, Redis, Celery, отдельный сервис отправки webhook, обязательный пятый daemon.
- Гарантия exactly-once для реального внешнего списания и для webhook-доставки.
- Промышленная платёжная платформа: только один микросервис с эмулятором шлюза.

## Capabilities

### New Capabilities

- `payment-api`: HTTP-контракт — создание платежа (идемпотентность, валидация, 202/409/422), чтение платежа
  (200/404), аутентификация по `X-API-Key` (401), защита служебных эндпоинтов, защищённые/отключённые docs.
- `payment-processing`: транзакционное создание Payment+Outbox, публикация через Outbox relay с publisher confirm,
  оркестрация consumer, эмулятор шлюза (бизнес-отказ vs техническая ошибка), переходы статусов pending→succeeded/failed,
  защита от конкурентной обработки, retry с устойчивым счётчиком попыток и Dead Letter Queue.
- `webhook-delivery`: контракт payload (event_id, payment_id, status, processed_at), таймаут и критерий успеха 2xx,
  устойчивое хранение состояния доставки отдельно от бизнес-статуса, доставка at-least-once и дедупликация получателем.
- `service-runtime`: конфигурация через окружение, Docker Compose (postgres, rabbitmq, api, consumer) с healthcheck/readiness,
  воспроизводимый шаг применения миграций, структурированное логирование и определение готовности сервисов.

### Modified Capabilities

<!-- Нет существующих спецификаций: проект greenfield. -->

## Impact

- Новый код: `app/` (HTTP-слой, прикладные сервисы, репозитории, publisher, эмулятор шлюза, webhook-клиент, consumer),
  `migrations/` (Alembic), `tests/` (unit, integration, e2e), `docker-compose.yml`, `Dockerfile`, `Makefile`,
  `pyproject.toml`, `README.md`, `.env.example`.
- Внешние зависимости: FastAPI, Pydantic v2, SQLAlchemy 2.0 (async), asyncpg, Alembic, FastStream + aio-pika, httpx,
  pytest, pytest-asyncio/AnyIO, pytest-cov, ruff, mypy.
- Инфраструктура: PostgreSQL и RabbitMQ как реальные сервисы в Compose; тесты integration/E2E требуют поднятой
  инфраструктуры и не подменяются SQLite или моками брокера.
- Контракты: новые HTTP-эндпоинты и RabbitMQ-топология; формат webhook payload. Breaking changes отсутствуют.

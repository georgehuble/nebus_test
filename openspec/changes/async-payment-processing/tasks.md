Матрица «требование → сценарий → задача → планируемый тест» находится в
[`design.md`](openspec/changes/async-payment-processing/design.md) (раздел Traceability Matrix).

## 1. Окружение и инструменты

- [x] 1.1 Инициализировать проект через `uv` (`pyproject.toml`, `uv.lock`), Python 3.12; проверка: `uv sync` завершается успешно и создаёт lock-файл
- [x] 1.2 Объявить зависимости (FastAPI, Pydantic v2, pydantic-settings, SQLAlchemy 2.0 async, asyncpg, Alembic, FastStream/aio-pika, httpx) и dev-зависимости (pytest, pytest-asyncio, pytest-cov, ruff, mypy, freezegun); проверка: `uv sync` ставит все пакеты без конфликтов
- [x] 1.3 Настроить Ruff (`E`,`F`,`I`,`B`,`UP`,`ASYNC`, line-length 120, он же форматтер) и mypy strict с точечными overrides; проверка: `ruff check .`, `ruff format --check .`, `mypy .` выполняются без ошибок на пустом/минимальном коде
- [x] 1.4 Настроить pytest (`pytest-asyncio`, единый `asyncio_mode=auto`, маркеры `unit`/`integration`/`e2e`, pytest-cov); проверка: `pytest -m unit` отрабатывает на временном тесте
- [x] 1.5 Создать скелет пакета `app/` (слои `api`, `application`, `infrastructure`, `gateway`) и `tests/`; проверка: импорт пакета не падает

## 2. Конфигурация, логирование и async-инфраструктура

- [x] 2.1 Реализовать `Settings` на pydantic-settings (`API_KEY`, `DATABASE_URL`, `RABBITMQ_URL`, таймауты, batch size, TTL) с падением при отсутствии обязательных переменных; проверка: unit-тест отсутствия переменной даёт понятную ошибку (`tests/unit/test_settings.py`)
- [x] 2.2 Реализовать async-движок и фабрику сессий SQLAlchemy (`async_sessionmaker`, `expire_on_commit=False`) и корректное закрытие ресурсов; проверка: unit-тест создаёт/закрывает сессию без блокирующих вызовов
- [x] 2.3 Настроить структурированное логирование с полями `payment_id`, `event_id`, номер попытки; проверка: unit-тест проверяет наличие полей и отсутствие значений ключа/метаданных
- [x] 2.4 Реализовать `Protocol`-ы `PaymentGateway`, `WebhookSender`, `RandomSource`, `Clock`/`Sleeper`, `EventPublisher` и простые реализации по умолчанию; проверка: mypy проходит, тестовые double подставляются без изменений в сценарии
- [x] 2.5 Создать `.env.example` с плейсхолдерами (без реальных секретов); проверка: unit-тест/команда подтверждает отсутствие значений, похожих на секреты

## 3. Доменные контракты

- [x] 3.1 Реализовать DTO платежа (Pydantic v2): `amount: Decimal` (строка в JSON, `> 0`, ≤ 2 знаков, `≤ 999999999.99`), `currency: Literal[RUB,USD,EUR]`, `description` (default `""`, ≤ 500 code points), `metadata` (default `{}`, ≤ 16384 байта канонического JSON), `webhook_url: HttpUrl`; проверка: unit-тесты валидации и сериализации
- [x] 3.2 Описать событие `payment.created` и `Protocol` publisher; проверка: unit-тест сериализации события содержит `event_id`, `payment_id`, `occurred_at`
- [x] 3.3 Реализовать эмулятор шлюза с инъекцией `RandomSource`/`Sleeper`: задержка 2–5 с, 90% успех / 10% бизнес-отказ, результат и задержка — детерминированная функция от `payment_id` (сид из UUID); проверка: unit-тесты обеих ветвей, границ задержки и повторного вызова
- [x] 3.4 Описать переходы статусов (`pending → succeeded|failed`, терминальность, `processed_at`); проверка: unit-тесты разрешённых и запрещённых переходов
- [x] 3.5 Реализовать сборку webhook-payload со стабильным `event_id`: UUIDv5 от фиксированной namespace-константы и канонического имени `webhook:<payment_id>:<status>` (lowercase); проверка: unit-тест одинакового `event_id` при повторной сборке и при использовании сохранённого значения

## 4. HTTP API и аутентификация

- [x] 4.1 Реализовать схемы запроса/ответа и эндпоинт `POST /api/v1/payments`; проверка: интеграционный тест валидного POST даёт `202` с `payment_id`/`status=pending`/`created_at`
- [x] 4.2 Реализовать dependency проверки `X-API-Key` через `compare_digest`; проверка: unit/integration-тест `401` при отсутствии и неверном ключе
- [x] 4.3 Реализовать эндпоинт `GET /api/v1/payments/{payment_id}` (`200`/`404`/`422`, `processed_at=null` для pending); проверка: интеграционные тесты всех кодов ответов
- [x] 4.4 Защитить всю HTTP-поверхность: отключить `/docs`,`/redoc`,`/openapi.json` и отдать защищённый `/api/v1/openapi.json`; все маршруты, включая health, требуют `X-API-Key`; проверка: интеграционный тест `401` для любого маршрута без ключа, включая health
- [x] 4.5 Реализовать единый обработчик ошибок, приводящий валидацию к `422`, а несовпадение отпечатка — к `409`; проверка: интеграционные тесты кодов ответов
- [x] 4.6 Реализовать key-protected `GET /api/v1/health`, проверяющий работающий процесс `api` (ASGI-сервер отвечает, БД доступна), без данных платежей; проверка: интеграционный тест health под ключом и без него (`tests/integration/test_health.py`)

## 5. Персистентность, миграции и запись Outbox

- [x] 5.1 Реализовать модели SQLAlchemy (`payments`, `outbox`) через `Mapped`/`mapped_column`: SQL-колонка `metadata` маппится на Python-атрибут `metadata_` (`mapped_column("metadata", ...)`), т.к. `metadata` на declarative-классе зарезервирован SQLAlchemy; delivery-state, `request_fingerprint`; отдельно поля публикации relay (`claimed_at`/`published_at`) и поля попыток consumer (`attempt_no`/`next_attempt_no`/`attempt_state`/`attempt_owner`/`attempt_epoch`/`attempt_lease_expires_at`/`attempt_heartbeat_at`/`next_attempt_at`/`recoveries`/`last_error`/`last_attempt_at`); CHECK только для `description` (metadata без DB CHECK) и `UNIQUE (payment_id, event_type)`; проверка: mypy проходит, поля соответствуют design D3
- [x] 5.2 Сгенерировать Alembic-миграции со всеми CHECK/UNIQUE/индексами (включая partial); проверка: `alembic upgrade head` на пустой PostgreSQL создаёт схему, `alembic downgrade base` её убирает
- [x] 5.3 Реализовать транзакционное создание Payment+Outbox и канонизацию отпечатка (сортировка ключей, defaults `description=''`/`metadata={}`, нормализация Decimal, измерение размера metadata по каноническому JSON); проверка: unit-тест нормализации + интеграционный тест атомарности
- [x] 5.4 Реализовать `INSERT ... ON CONFLICT DO NOTHING RETURNING` с разрешением гонки и ветками `202`/`409`; проверка: интеграционный тест дубля и конфликта
- [x] 5.5 Реализовать репозитории с короткими транзакциями и владением сессией (без шаринга сессии между задачами); проверка: unit/integration-тест отсутствия долгих транзакций и блокирующих вызовов
- [x] 5.6 Реализовать устойчивое хранение delivery-state (`webhook_event_id`, `webhook_status`, `webhook_attempts`, `webhook_last_error`) с условным терминальным апдейтом; проверка: интеграционный тест «один победитель» и неизменяемости терминала

## 6. Outbox relay, топология и consumer

- [x] 6.1 Реализовать publisher поверх FastStream/aio-pika с publisher confirm и `mandatory=True`; проверка: интеграционный тест подтверждённой публикации
- [x] 6.2 Объявить durable-топологию без плагинов: exchange `payments`, **quorum**-очередь `payments.new`, exchange `payments.dlx`, **quorum**-очередь `payments.new.dlq`; TTL retry-очереди не используются (retry планируется в БД, задача 6.11); проверка: интеграционный тест проверяет фактические exchanges/queues (тип quorum) и bindings
- [x] 6.3 Реализовать relay как фоновую задачу consumer: `SELECT ... WHERE published_at IS NULL AND (claimed_at IS NULL OR claimed_at < now() - :lease) FOR UPDATE SKIP LOCKED` батчем, простановка `claimed_at`, публикация вне транзакции, отметка `published_at` только после confirm; проверка: интеграционный тест отсутствия двойной публикации при двух relay и восстановления при недоступности брокера
- [x] 6.4 Реализовать обработчик consumer в точном порядке: захват работы (lease, без смены статуса) → старт попытки → эмулятор шлюза → условное сохранение терминального результата (отдельно от захвата) → webhook → сохранение доставки → `ack`; проверка: unit/integration-тест порядка операций и разделения захвата и статуса
- [x] 6.5 Реализовать захват с лизом, heartbeat и fencing: `attempt_no` инкрементируется атомарно в одной инструкции с захватом (нет `attempt_no = 0`/`retry.0`), recovery/takeover не растит `attempt_no` и повышает `attempt_epoch`, записи старого владельца отвергаются эпохой, при `x-attempt != ожидаемому` доставка считается дублем, активный чужой лиз не ACK-ается без durable-копии, сохранённое состояние проверяется до оценки бюджета, DLQ только для незавершённой работы; проверка: интеграционный тест различения дубля/recovery/`awaiting_retry`, takeover и отсутствия потери
- [x] 6.6 Реализовать webhook-клиент (httpx, таймаут, `2xx`, без redirects) и продолжение доставки из сохранённого результата без повторного вызова шлюза; проверка: unit-тест классификации + интеграционный тест доставки
- [x] 6.7 Реализовать обработку невалидного сообщения и неизвестного `payment_id` без redelivery-петли; проверка: интеграционный тест детерминированной обработки обоих случаев
- [x] 6.8 Реализовать запуск/останов фоновых задач в lifespan приложения; проверка: интеграционный тест чистого shutdown без висящих ресурсов
- [x] 6.9 Реализовать readiness consumer: локальный key-protected health, проверяющий, что запущены задачи обработчика и relay и что подключения к БД и брокеру живы; проверка: интеграционный/ручной тест readiness при живых и остановленных задачах
- [x] 6.10 Реализовать heartbeat-продление лизы и fencing: периодический апдейт `attempt_lease_expires_at`/`attempt_heartbeat_at` под `attempt_owner`/`attempt_epoch`, takeover истёкшего лизы с `attempt_epoch++`, остановка прежнего владельца при несовпадении эпохи; проверка: интеграционный тест takeover и отсутствия второго терминального результата
- [x] 6.11 Реализовать retry dispatcher: выбор `attempt_state='awaiting_retry' AND next_attempt_at <= now()` через `FOR UPDATE SKIP LOCKED`, публикация с `x-attempt = next_attempt_no` (publisher confirm + `mandatory`), очистка `next_attempt_at` только после confirm, безопасная повторная публикация при падении до очистки; проверка: интеграционный тест доставки запланированного retry и отсутствия потери при падении dispatcher

## 7. Unit-тесты

- [x] 7.1 Покрыть деньги/валюты/даты и их сериализацию (включая строковый `amount`, tz-aware UTC и границу `amount` 999999999.99); проверка: `pytest -m unit tests/unit/test_money_validation.py` зелёный
- [x] 7.2 Покрыть нормализацию отпечатка и разрешённые переходы статусов; проверка: `pytest -m unit tests/unit/test_status_transitions.py` зелёный
- [x] 7.3 Покрыть обе ветви эмулятора, границы задержки 2–5 с и детерминированный повтор (тот же результат при повторном вызове для того же `payment_id`); проверка: `pytest -m unit tests/unit/test_gateway_emulator.py` зелёный
- [x] 7.4 Покрыть retry по попыткам: успех сразу, после 1–2 ошибок, исчерпание, сопоставление `x-attempt` (законная попытка vs старый дубль), fencing по `attempt_epoch`, падение на последней попытке (без реальных задержек); проверка: `pytest -m unit tests/unit/test_retry_policy.py` зелёный
- [x] 7.5 Покрыть различие бизнес-ошибки и ошибки доставки и отсутствие повторного вызова шлюза при повторе webhook; проверка: `pytest -m unit tests/unit/test_error_classification.py` зелёный
- [x] 7.6 Покрыть классификацию HTTP-ответов webhook и `event_id` (UUIDv5 от фиксированного namespace и канонического имени, стабильность при повторе и рестарте); проверка: `pytest -m unit tests/unit/test_webhook_client.py tests/unit/test_webhook_payload.py` зелёный
- [x] 7.7 Покрыть границы `description` (500/501 code point) и `metadata` (ровно 16384 и 16385 байт канонического JSON); проверка: `pytest -m unit tests/unit/test_field_limits.py` зелёный
- [x] 7.8 Покрыть fail-fast настройки и структурированное логирование (корреляция; без API-ключа и полных метаданных); проверка: `pytest -m unit tests/unit/test_settings.py tests/unit/test_logging.py` зелёный
- [x] 7.9 Покрыть границы async (отсутствие блокирующих вызовов и шаринга сессии) и чистый останов фоновых задач; проверка: `pytest -m unit tests/unit/test_async_boundaries.py` зелёный

## 8. Интеграционные тесты (реальные PostgreSQL и RabbitMQ)

- [x] 8.1 Реализовать fixtures: реальные PG и RabbitMQ, применение миграций, тестовый webhook-receiver (test-profile); проверка: fixture поднимается, миграции применяются, receiver отвечает
- [x] 8.2 Покрыть API: создание/получение, аутентификация, все коды (`202/200/401/404/409/422`); проверка: `pytest -m integration tests/integration/test_payments_api.py` зелёный
- [x] 8.3 Покрыть rollback создания (нет ни платежа, ни outbox-события); проверка: `pytest -m integration tests/integration/test_create_atomicity.py` зелёный
- [x] 8.4 Покрыть идемпотентность: тот же ключ/то же тело, тот же ключ/другое тело; проверка: `tests/integration/test_idempotency.py` зелёный
- [x] 8.5 Покрыть конкурентные POST (ровно один платёж и одно outbox-событие); проверка: `tests/integration/test_idempotency.py::test_concurrent` зелёный
- [x] 8.6 Покрыть реальную публикацию Outbox, publisher confirm и восстановление после недоступности брокера; проверка: `tests/integration/test_outbox_relay.py` зелёный
- [x] 8.7 Покрыть окно повторной публикации, redelivery и конкурентный дубль сообщения; проверка: `tests/integration/test_outbox_relay.py` зелёный
- [x] 8.8 Покрыть стабильность результата при сбое до/после сохранения статуса; проверка: `tests/integration/test_concurrent_processing.py` зелёный
- [x] 8.9 Покрыть на реальных PostgreSQL и RabbitMQ: активный чужой лиз → durable-копия (нет потери), takeover по heartbeat и fencing (старый владелец остановлен), различение дубля/recovery/`awaiting_retry` по `x-attempt`, успешная третья попытка с падением до `ack`, падение на последней попытке, retry dispatcher с publisher confirm, DLQ (quorum at-least-once) только для незавершённой работы; проверка: `tests/integration/test_retry_dlq.py` и `tests/integration/test_consumer_lease.py` зелёные
- [x] 8.10 Покрыть webhook `2xx`, `non-2xx`, таймаут и crash window после приёма получателем; проверка: `tests/integration/test_webhook_delivery.py` зелёный
- [x] 8.11 Покрыть применение миграций к пустой PostgreSQL и итоговую схему; проверка: `tests/integration/test_migrations.py` зелёный
- [x] 8.12 Покрыть границы metadata на реальном PostgreSQL: ровно 16384 и 16385 байт UTF-8 компактного канонического JSON, включая много-байтовый Unicode, несколько ключей и числовые значения; проверка: `tests/integration/test_api_metadata_limits.py` зелёный

## 9. E2E-тесты

- [x] 9.1 Реализовать E2E POST → outbox → RabbitMQ → consumer → финальный статус → тестовый webhook → GET; проверка: `pytest -m e2e tests/e2e/test_payment_flow.py` зелёный
- [x] 9.2 Реализовать E2E сценарий исчерпания попыток и проверки DLQ; проверка: `pytest -m e2e tests/e2e/test_dlq_flow.py` зелёный

## 10. Docker Compose и quality gates

- [x] 10.1 Реализовать `Dockerfile` для `api` и `consumer`; проверка: `docker compose build` успешен
- [x] 10.2 Реализовать `docker-compose.yml` с `postgres`, `rabbitmq`, `api`, `consumer`: healthchecks (для `api` — вызов key-protected health из контейнера; для `consumer` — проверка живых задач обработчика/relay и подключений), имена сервисов, volumes, depends_on по готовности, корректная остановка; проверка: `docker compose up -d` поднимает все четыре сервиса здоровыми
- [x] 10.3 Зафиксировать единственный шаг миграций (`docker compose run --rm api alembic upgrade head`), без `create_all` и без параллельных миграций; проверка: миграции применяются к пустой БД в compose
- [x] 10.4 Реализовать `Makefile`: `make check` (ruff lint, ruff format --check, mypy, pytest) и отдельную команду поднятия инфраструктуры + integration/E2E; проверка: обе команды выполняются по документации
- [x] 10.5 Замерить покрытие ветвей и строк и приложить отчёт; проверка: `pytest --cov=app --cov-report=term-missing` показывает обязательные сценарии покрытыми
- [x] 10.6 Подключить healthchecks, проверяющие работающий процесс: для `api` — вызов key-protected `/api/v1/health` из контейнера, для `consumer` — проверка readiness живых задач handler/relay и подключений к БД/брокеру; «импорт + `SELECT 1`» как readiness не используется; проверка: `docker compose ps` показывает healthy только при работающих задачах и доступных зависимостях

## 11. Рефакторинг после работающего сценария

- [x] 11.1 Убрать дублирование и раздутые функции, сохранив зелёные тесты (не меняя контракты без оговорки); проверка: `make check` зелёный до и после
- [x] 11.2 Проверить границы слоёв (api → application → infrastructure/gateway) и отсутствие циклических импортов; проверка: статический анализ импортов и ревью диффа
- [x] 11.3 Убедиться, что введённые абстракции оправданы (Protocol вместо фабрик, без универсальных репозиториев); проверка: ревью диффа и отсутствие неиспользуемых слоёв
- [x] 11.4 Добавить docstrings для публичных компонентов и нетривиальных гарантий; проверка: `ruff`/`mypy` зелёные, ревью docstrings

## 12. Документация и Definition of Done

- [x] 12.1 Написать README: `.env`, build/start, миграции, curl для обоих эндпоинтов, повтор с тем же ключом, конфликт, webhook payload (включая `event_id`), просмотр DLQ, лимиты amount/description/metadata, key-protected health и отсутствие анонимных маршрутов, запуск проверок, схема Outbox/retry, порядок захват→результат→доставка→ack и ограничения гарантий; проверка: ревью README проходит сценарий независимого запуска
- [x] 12.2 Задокументировать сетевое ограничение (`localhost` внутри consumer = сам контейнер) и адрес тестового webhook, доступный из контейнера; проверка: ревью README
- [x] 12.3 Сопоставить тесты с требованиями по матрице трассируемости и убедиться в отсутствии заглушек обязательного поведения; проверка: сверка с Traceability Matrix, отсутствие постоянных `skip` на обязательных тестах
- [x] 12.4 Выполнить финальный прогон `make check` и integration/E2E и разнести результаты на выполненные, упавшие и не запущенные проверки; проверка: отчёт с разделением статусов

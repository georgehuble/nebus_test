# Async Payment Processing

> Привет ревьюер у вас хорошее максимальное подробное ТЗ, hr сказала потратить на эту задачу пару часов поэтому я закинул это в нейронку. Ручками по чистой архитектуре так быстро не справиться

Асинхронный микросервис процессинга платежей: **FastAPI + Pydantic v2 + SQLAlchemy 2.0 (async) + PostgreSQL + RabbitMQ (aio-pika) + Alembic**, запуск через Docker Compose.

Сервис принимает запланированную оплату по HTTP, идемпотентно сохраняет её вместе с событием Outbox в одной транзакции и асинхронно обрабатывает через эмулятор внешнего шлюза. Итог фиксируется в терминальном статусе и доставляется клиенту по webhook; повторы ограничены бюджетом попыток, незавершённая работа уходит в Dead Letter Queue.

[![CI](https://github.com/georgehuble/nebus_test/actions/workflows/ci.yml/badge.svg)](https://github.com/georgehuble/nebus_test/actions/workflows/ci.yml)
[![Publish container image](https://github.com/georgehuble/nebus_test/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/georgehuble/nebus_test/actions/workflows/docker-publish.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/release/python-3120/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](https://mypy-lang.org/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](docker-compose.yml)

---

## Запуск

```bash
cp .env.example .env                                  # конфигурация окружения
docker compose up -d --wait postgres rabbitmq         # инфраструктура + healthchecks
docker compose run --rm api alembic upgrade head      # миграции
docker compose up -d --build api consumer             # приложение
docker compose ps                                     # все сервисы должны быть healthy
```

`api` — `http://localhost:8000`. PostgreSQL и RabbitMQ публикуются на порты `15432` и `15673`, чтобы не конфликтовать с локальными сервисами.

Остановка: `docker compose down` (данные сохраняются) или `docker compose down -v` (чистое состояние).

## Конфигурация

Настройки читаются из окружения (`.env`). Обязательные переменные: `API_KEY`, `DATABASE_URL`, `RABBITMQ_URL` — при их отсутствии сервис падает на старте. Остальные параметры (`MAX_ATTEMPTS=3`, `MAX_RECOVERIES=5`, `RETRY_BASE_DELAY_SECONDS=1.0`, `ATTEMPT_LEASE_SECONDS=15.0`, `WEBHOOK_TIMEOUT_SECONDS=10.0`, `LOG_LEVEL=INFO` и др.) имеют значения по умолчанию — полный список в [`app/settings.py`](app/settings.py).

## HTTP API

Все маршруты требуют заголовок `X-API-Key`. Служебные `/docs`, `/redoc` и `/openapi.json` отключены; контракт доступен по защищённому `GET /api/v1/openapi.json`.

```bash
export API_KEY=dev-api-key-change-me
BASE=http://localhost:8000

# создание платежа (202 Accepted после commit транзакции)
curl -sS -X POST "$BASE/api/v1/payments" \
  -H "X-API-Key: $API_KEY" -H "Idempotency-Key: demo-key-1" \
  -H "Content-Type: application/json" \
  -d '{"amount":"10.50","currency":"RUB","description":"Заказ 42",
       "metadata":{"order_id":"42"},
       "webhook_url":"http://host.docker.internal:9000/hook"}'

# чтение платежа
curl -sS "$BASE/api/v1/payments/<payment_id>" -H "X-API-Key: $API_KEY"

# health
curl -sS "$BASE/api/v1/health" -H "X-API-Key: $API_KEY"
```

Лимиты: `amount` — строка `> 0` с не более 2 знаками после запятой (до `999999999.99`); `currency` — `RUB`/`USD`/`EUR`; `description` — до 500 символов; `metadata` — до 16 KiB канонического JSON; `webhook_url` — абсолютный `http`/`https` URL. Повтор с тем же `Idempotency-Key` и телом возвращает тот же платёж (`202`), с другим телом — `409`. Коды чтения: `200`, `404`, `422`, `401`.

## Архитектура

Слои в `app/`: `api/` (FastAPI, роутеры, схемы, проверка `X-API-Key`), `application/` (сценарии и `PaymentProcessor`), `domain/` (DTO, события, состояния, webhook payload), `infrastructure/` (SQLAlchemy-модели и репозитории, Outbox relay, retry dispatcher, webhook-клиент), `gateway/` (детерминированный эмулятор шлюза), `consumer/` (цикл обработчика, readiness). Заменяемые зависимости описаны `Protocol` и внедряются через конструктор; композиционный корень — [`app/container.py`](app/container.py).

```
POST /api/v1/payments ──► одна транзакция: payments(pending) + outbox(payment.created)
Outbox relay ──► RabbitMQ payments.new ──► Consumer: lease → шлюз → терминальный статус → webhook → ack
Техническая ошибка ──► durable retry в БД ──► повторная публикация с x-attempt
Бюджет исчерпан ──► payments.new.dlq
```

- **Гарантии — at-least-once, не exactly-once**: возможны повторы публикации и webhook-доставки. Consumer идемпотентен по `event_id`; получатель webhook дедуплицирует по `event_id` (UUIDv5, стабильный между рестартами).
- **Захват попытки**: lease с продлением heartbeat, fencing через `attempt_epoch`; takeover не расходует бюджет.
- **Retry** планируется в БД (`next_attempt_at`, задержки 1 с и 2 с) и публикуется dispatcher'ом с заголовком `x-attempt`.
- Незавершённая работа после исчерпания бюджета/лимита recovery уходит в `payments.new.dlq` (quorum-очереди, без плагинов).
- Webhook: `POST` на `webhook_url`, успех — только `2xx`, redirects не следуются. Для получателя на хосте используйте `http://host.docker.internal:<PORT>/hook` (внутри контейнера `localhost` указывает на сам consumer).

## Проверки

```bash
make check         # ruff lint + ruff format --check + mypy (strict) + unit-тесты
make integration   # реальные PostgreSQL и RabbitMQ: integration + e2e
make coverage      # покрытие ветвей и строк
```

`pytest -m unit` не требует инфраструктуры; `pytest -m integration` и `pytest -m e2e` работают на реальных PostgreSQL и RabbitMQ (SQLite и моки брокера не используются). CI ([`ci.yml`](.github/workflows/ci.yml)) запускает качество, unit-, integration-тесты и сборку Docker-образа; пуш тега `vX.Y.Z` публикует образ в GHCR ([`docker-publish.yml`](.github/workflows/docker-publish.yml)).

## Структура

```
(`http://localhost:8001` внутри контейнера; наружу порт не публикуется).

Остановка и очистка:

```bash
docker compose down         # остановить, сохранив данные в volumes
docker compose down -v      # остановить и удалить данные (чистое состояние)
```

### Порты хоста

PostgreSQL и RabbitMQ публикуются на порты, отличные от стандартных, чтобы не конфликтовать с
уже запущенными сервисами: `15432` (PostgreSQL) и `15673` (AMQP). Переопределить можно так:

```bash
POSTGRES_HOST_PORT=5432 RABBITMQ_HOST_PORT=5672 docker compose up -d --wait
```

---

## 4. Конфигурация

Все настройки читаются из окружения (`.env` или переменные процесса). Отсутствие обязательной
переменной приводит к быстрому падению на старте.

| Переменная | Обязательна | По умолчанию | Назначение |
|---|---|---|---|
| `API_KEY` | да | — | статический ключ `X-API-Key` |
| `DATABASE_URL` | да | — | строка `postgresql+asyncpg://…` |
| `RABBITMQ_URL` | да | — | строка `amqp://…` |
| `MAX_ATTEMPTS` | нет | `3` | бюджет попыток обработки сообщения |
| `MAX_RECOVERIES` | нет | `5` | допустимое число takeover одной попытки |
| `RETRY_BASE_DELAY_SECONDS` | нет | `1.0` | база экспоненциальной задержки (1 с, 2 с) |
| `GATEWAY_MIN_DELAY_SECONDS` / `GATEWAY_MAX_DELAY_SECONDS` | нет | `2.0` / `5.0` | окно эмуляции задержки шлюза |
| `GATEWAY_DECLINE_PROBABILITY` | нет | `0.10` | вероятность бизнес-отказа |
| `ATTEMPT_LEASE_SECONDS` | нет | `15.0` | лиз захвата попытки |
| `HEARTBEAT_INTERVAL_SECONDS` | нет | `3.0` | период продления лиза |
| `RELAY_CLAIM_LEASE_SECONDS` | нет | `60.0` | лиз claim записи Outbox |
| `RELAY_BATCH_SIZE` | нет | `50` | размер батча relay |
| `RELAY_POLL_INTERVAL_SECONDS` / `RETRY_POLL_INTERVAL_SECONDS` | нет | `1.0` / `0.5` | периоды фоновых циклов |
| `WEBHOOK_TIMEOUT_SECONDS` | нет | `10.0` | таймаут webhook-запроса |
| `LOG_LEVEL` | нет | `INFO` | уровень логирования |
| `SERVICE_NAME` | нет | `payment-service` | имя сервиса в логах |

---

## 5. Аутентификация

**Все** HTTP-маршруты требуют заголовок `X-API-Key` (сравнение через `secrets.compare_digest`).
Анонимных HTTP-маршрутов нет: служебные `/docs`, `/redoc` и `/openapi.json` отключены, а
сгенерированный контракт доступен по защищённому `GET /api/v1/openapi.json`. Health-эндпоинты
`api` и `consumer` тоже защищены ключом.

```bash
export API_KEY=dev-api-key-change-me
BASE=http://localhost:8000
```

---

## 6. HTTP API

Лимиты контракта:

- `amount` — JSON-строка, `> 0`, не более 2 знаков после запятой, не более `999999999.99`
  (без молчаливого округления);
- `currency` — `RUB` | `USD` | `EUR`;
- `description` — необязательно, по умолчанию `""`, не более 500 Unicode code points;
- `metadata` — необязательно, по умолчанию `{}`, не более 16384 байт (16 KiB) UTF-8
  канонического JSON (`sort_keys=True`, компактные разделители, `ensure_ascii=False`);
- `webhook_url` — абсолютный `http`/`https` URL.

### 6.1 Создание платежа

```bash
curl -sS -X POST "$BASE/api/v1/payments" \
  -H "X-API-Key: $API_KEY" \
  -H "Idempotency-Key: demo-key-1" \
  -H "Content-Type: application/json" \
  -d '{
        "amount": "10.50",
        "currency": "RUB",
        "description": "Заказ 42",
        "metadata": {"order_id": "42"},
        "webhook_url": "http://host.docker.internal:9000/hook"
      }'
# => 202 {"payment_id":"…","status":"pending","created_at":"…"}
```

Ответ `202 Accepted` выдаётся только после commit транзакции, поэтому недоступность брокера
не теряет принятый платёж.

### 6.2 Идемпотентный повтор (тот же ключ, то же тело)

```bash
curl -sS -X POST "$BASE/api/v1/payments" \
  -H "X-API-Key: $API_KEY" -H "Idempotency-Key: demo-key-1" \
  -H "Content-Type: application/json" \
  -d '{"amount":"10.50","currency":"RUB","description":"Заказ 42",
       "metadata":{"order_id":"42"},"webhook_url":"http://host.docker.internal:9000/hook"}'
# => 202 с тем же payment_id и created_at, новых строк не создаётся
```

### 6.3 Конфликт (тот же ключ, другое тело)

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE/api/v1/payments" \
  -H "X-API-Key: $API_KEY" -H "Idempotency-Key: demo-key-1" \
  -H "Content-Type: application/json" \
  -d '{"amount":"99.00","currency":"RUB","metadata":{},
       "webhook_url":"http://host.docker.internal:9000/hook"}'
# => 409
```

### 6.4 Чтение платежа

```bash
curl -sS "$BASE/api/v1/payments/<payment_id>" -H "X-API-Key: $API_KEY"
# => 200 {"payment_id":"…","amount":"10.50","currency":"RUB","description":"…",
#         "metadata":{…},"status":"succeeded","webhook_url":"…",
#         "created_at":"…","processed_at":"…"}
```

Коды: `200` найден, `404` неизвестный UUID, `422` некорректный UUID или тело, `401` без ключа.

### 6.5 Health и контракт

```bash
curl -sS "$BASE/api/v1/health" -H "X-API-Key: $API_KEY"          # 200 {"status":"ok","database":"ok"}
curl -sS "$BASE/api/v1/openapi.json" -H "X-API-Key: $API_KEY"    # сгенерированный контракт
```

---

## 7. Webhook-уведомление

После фиксации терминального статуса сервис выполняет `POST` на `webhook_url` с JSON:

```json
{
  "event_id": "8f1d9c2e-…-9a0b",
  "payment_id": "…",
  "status": "succeeded",
  "processed_at": "2026-10-05T13:41:56.146808Z"
}
```

- `Content-Type: application/json`; успех — только `2xx`; redirects не следуются;
  таймаут задаётся `WEBHOOK_TIMEOUT_SECONDS`.
- `event_id` — **UUIDv5** от фиксированного namespace проекта и канонического имени
  `webhook:<payment_id>:<status>` (lowercase). Значение вычисляется один раз при первом
  терминальном переходе, сохраняется в `payments.webhook_event_id` и повторно используется,
  поэтому один логический webhook всегда несёт один и тот же `event_id` — даже после рестарта.
- Гарантия доставки — **at-least-once**: падение после успешного ответа получателя, но до
  сохранения отметки, приводит к повторной доставке с тем же `event_id`. Получатель должен
  дедуплицировать по `event_id`.

### 7.1 Сеть: `localhost` внутри контейнера

`localhost` в URL webhook, обрабатываемом **consumer**, указывает на сам контейнер consumer, а не
на вашу машину. Для получателя, запущенного на хосте, используйте адрес, доступный из контейнера:
`http://host.docker.internal:<PORT>/hook` (сервисы `api` и `consumer` уже имеют
`extra_hosts: host.docker.internal:host-gateway`).

Пример быстрого приёмника на хосте:

```bash
python -m http.server 9000            # либо любой HTTP-приёмник на /hook
# webhook_url = http://host.docker.internal:9000/hook
```

### 7.2 Просмотр DLQ

Метрики очередей:

```bash
docker compose exec rabbitmq rabbitmqctl list_queues name messages consumers
```

Выгрузка сообщений мёртвой очереди `payments.new.dlq` (внутри compose-сети):

```bash
docker compose exec api python -c "
import asyncio, json, aio_pika
async def main():
    conn = await aio_pika.connect_robust('amqp://guest:guest@rabbitmq:5672/')
    ch = await conn.channel()
    q = await ch.declare_queue('payments.new.dlq', passive=True)
    n = q.declaration_result.message_count
    print('DLQ messages:', n)
    for _ in range(n):
        m = await q.get(no_ack=False)
        if m is None:
            break
        print(json.dumps(json.loads(m.body), ensure_ascii=False))
        await m.ack()
    await conn.close()
asyncio.run(main())
"
```

Сообщение DLQ содержит `event_id`, `payment_id`, `attempt_no`, `reason`, `failed_at`.

---

## 8. Outbox, retry и порядок обработки

- **Публикация**: relay — фоновая задача в процессе consumer. Короткая транзакция помечает
  записи `claimed_at` (`FOR UPDATE SKIP LOCKED` + лиз), публикация выполняется **вне**
  транзакции с publisher confirm и `mandatory=True`, отметка `published_at` — только после
  подтверждения. Успешно опубликованные записи не удаляются, а остаются помеченными.
- **Попытки**: `attempt_no` инкрементируется **той же SQL-инструкцией**, что и захват попытки,
  поэтому значение никогда не равно 0. Дубли, takeover и «старые» доставки бюджет не расходуют.
- **Расписание retry хранится в БД** (`next_attempt_at`), а не в broker TTL: retry dispatcher
  публикует сообщение позже с заголовком `x-attempt = next_attempt_no` и подтверждением. Задержки
  экспоненциальные: 1 с, затем 2 с.
- **Порядок одного прохода**: захват (lease, без смены статуса) → старт попытки → вызов шлюза →
  условное сохранение терминального результата (отдельный апдейт) → webhook → сохранение доставки
  → `ack`. Захват не меняет статус, а терминальный апдейт не выдаёт право на работу.
- **Fencing**: каждый takeover увеличивает `attempt_epoch`; записи прежнего владельца отвергаются,
  и он обязан немедленно остановиться.
- **DLQ**: незавершённая работа после исчерпания бюджета/лимита recovery уходит в `payments.new.dlq`
  (ограниченные очереди-`quorum`, at-least-once dead-lettering, без плагинов).

---

## 9. Проверки качества и CI/CD

```bash
# Единый гейт: Ruff lint, Ruff format --check, mypy, unit-тесты
make check

# Поднять PostgreSQL и RabbitMQ, применить миграции, прогнать integration и E2E
make integration

# Покрытие ветвей и строк
make coverage
```

`pytest -m unit` не требует инфраструктуры; `pytest -m integration` и `pytest -m e2e` работают на
**реальных** PostgreSQL и RabbitMQ (SQLite и моки брокера не используются). Обязательные
интеграционные тесты не скрыты за постоянным `skip`.

### 9.1 Непрерывная интеграция

Каждый push в `main` и каждый pull request запускают workflow
[`ci.yml`](.github/workflows/ci.yml) с четырьмя независимыми задачами:

| Задача | Что проверяет |
|---|---|
| `quality` | `ruff check`, `ruff format --check`, `mypy` (strict) |
| `unit` | `pytest -m unit` с покрытием и артефактом `coverage.xml` |
| `integration` | `pytest -m "integration or e2e"` на сервисных контейнерах PostgreSQL 16 и RabbitMQ 3.13 |
| `docker` | сборка production-образа из `Dockerfile` (Buildx, кеш GHA) |

Зависимости устанавливаются через `uv sync --all-groups --frozen`, поэтому проверки
воспроизводимы по `uv.lock`, а параллельные запуски одного ref отменяются.

### 9.2 Непрерывная доставка

Пуш тега вида `vX.Y.Z` запускает
[`docker-publish.yml`](.github/workflows/docker-publish.yml): образ собирается и публикуется
в GitHub Container Registry (`ghcr.io/georgehuble/nebus_test`) с тегами semver (`X.Y.Z`, `X.Y`, `X`)
и `latest`. Аутентификация выполняется через короткоживущий `GITHUB_TOKEN`; долгоживущие секреты
реестра не хранятся в репозитории.

### 9.3 Локальные pre-commit хуки

```bash
uv run pre-commit install                 # включить хуки в .git/hooks
uv run pre-commit run --all-files         # прогнать все хуки вручную
```

Хуки в [`.pre-commit-config.yaml`](.pre-commit-config.yaml) повторяют гейт `quality`
(ruff, ruff-format, проверки YAML/TOML/whitespace и строгий mypy).

---

## 10. Ограничения гарантий

- **At-least-once, не exactly-once.** Как для публикации Outbox, так и для доставки webhook
  возможны повторы (окна падения описаны в `design.md`). Consumer идемпотентен по `event_id` и
  условному терминальному апдейту; webhook-получатель дедуплицирует по `event_id`.
- **Эмулятор шлюза** даёт детерминированный результат и задержку как функцию `payment_id`, но это
  не делает реальное внешнее списание exactly-once: для настоящего провайдера нужен его
  idempotency key (вне области решения).
- **Незавершённая работа** может остаться в `pending` при трёх технических неудачах — сообщение
  гарантированно попадает в DLQ, где его можно разобрать и переопубликовать вручную.
- Сервис эмулирует один микросервис: без фронтенда, JWT, Kubernetes, Redis и Celery.

---

## 11. Структура репозитория

```
app/                     код сервиса (api, application, domain, infrastructure, gateway, consumer)
migrations/              Alembic (async), единственная начальная миграция
tests/unit/              быстрые изолированные тесты
tests/integration/       тесты на реальных PostgreSQL и RabbitMQ
tests/e2e/               сквозные сценарии полного стека
.github/workflows/       CI (lint, типы, тесты, сборка образа) и CD (публикация в GHCR)
docker-compose.yml       postgres, rabbitmq, api, consumer
Dockerfile               общий образ для api и consumer
Makefile                 гейты качества и запуск инфраструктуры
.env.example             пример конфигурации без секретов
.pre-commit-config.yaml  локальные хуки качества
.editorconfig            единый стиль редактирования
CONTRIBUTING.md          правила участия и локальный workflow
SECURITY.md              политика раскрытия уязвимостей
CODE_OF_CONDUCT.md       кодекс поведения Contributor Covenant
CHANGELOG.md             история изменений (Keep a Changelog)
LICENSE                  лицензия MIT
openspec/changes/async-payment-processing/   proposal, design, specs, tasks
```

Трассируемость «требование → сценарий → задача → тест» приведена в
[`openspec/changes/async-payment-processing/design.md`](openspec/changes/async-payment-processing/design.md).

---

## 12. Лицензия и участие

Проект распространяется под лицензией [MIT](LICENSE).

- Правила участия, локальный workflow и конвенции коммитов — в [`CONTRIBUTING.md`](CONTRIBUTING.md).
- Политика безопасного раскрытия уязвимостей — в [`SECURITY.md`](SECURITY.md).
- История изменений — в [`CHANGELOG.md`](CHANGELOG.md).
- Общение регулируется [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

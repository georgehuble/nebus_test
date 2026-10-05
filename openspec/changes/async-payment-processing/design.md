## Context

Greenfield-проект: репозиторий содержит только каркас OpenSpec (`openspec/config.yaml`, пустые `changes/` и `specs/`),
кода и зависимостей нет. Мотивация и границы — в [`proposal.md`](openspec/changes/async-payment-processing/proposal.md);
наблюдаемое поведение — в спецификациях [`payment-api`](openspec/changes/async-payment-processing/specs/payment-api/spec.md),
[`payment-processing`](openspec/changes/async-payment-processing/specs/payment-processing/spec.md),
[`webhook-delivery`](openspec/changes/async-payment-processing/specs/webhook-delivery/spec.md) и
[`service-runtime`](openspec/changes/async-payment-processing/specs/service-runtime/spec.md). Этот документ не повторяет
требования, а фиксирует технические решения и их обоснование.

Жёсткие ограничения: обязательный стек (FastAPI, Pydantic v2, SQLAlchemy 2.0 async, PostgreSQL, RabbitMQ + FastStream,
Alembic, Docker Compose), ровно один consumer, максимум четыре обязательных compose-сервиса, отсутствие Redis/Celery/
Kubernetes/фронтенда, at-least-once без недоказанного exactly-once.

## Goals / Non-Goals

**Goals:**

- Обеспечить принимаемому платежу надёжную асинхронную обработку: ни один успешно принятый запрос не теряется и
  приходит к наблюдаемому терминальному статусу либо к DLQ.
- Сделать недетерминизм (задержка, вероятность 90/10, время сети) управляемым и тестируемым без ожидания реальных
  секунд и без флаки из-за вероятности.
- Дать малым числом компонентов чистую слоистость: HTTP, прикладной сценарий, доступ к БД, публикация, эмулятор шлюза,
  webhook-клиент и один оркестрирующий обработчик.
- Подготовить полный воспроизводимый контур проверки (unit, integration, E2E) на реальных PostgreSQL и RabbitMQ.

**Non-Goals (design-level):**

- Гарантия exactly-once для внешнего списания и для webhook-доставки.
- Отдельный daemon для relay, отдельная таблица для состояния webhook, универсальные репозитории и абстрактные фабрики.
- Промышленные механизмы (идемпотентные ключи внешнего провайдера, шифрование метаданных, мультитенантность).

## Decisions

### D1. Слоистость и внедрение зависимостей

Слои: `api/` (роутеры, схемы Pydantic, dependency для проверки ключа), `application/` (сценарии: создание платежа,
чтение, обработка сообщения), `infrastructure/` (SQLAlchemy-модели и репозитории, outbox-relay, publisher, webhook-клиент),
`gateway/` (эмулятор). Обработчик consumer не содержит бизнес-логики: он вызывает сценарий `process_payment`.

Заменяемые зависимости объявляются как небольшие `Protocol`: `PaymentGateway` (эмулятор), `WebhookSender` (HTTP-клиент),
`RandomSource` (источник случайности), `Clock`/`Sleeper` (время и ожидание), `EventPublisher` (публикация в брокер).
Конкретные реализации инжектируются через конструктор. Композиция вместо наследования: реализации не наследуют
друг друга, а реализуют узкий протокол.

Альтернатива — прямые зависимости на httpx/random/asyncio внутри сценария — отклонена: делает тесты на реальные
секунды и вероятности флаки. Альтернатива — DI-контейнер — отклонена по YAGNI.

### D2. Идемпотентность и канонический отпечаток

У `payments` уникальное ограничение `UNIQUE (idempotency_key)`. Создание выполняется одной транзакцией:
`INSERT ... ON CONFLICT (idempotency_key) DO NOTHING RETURNING id`. Если строка вставлена — создаём outbox-событие и
коммитим. Если конфликт — читаем существующую строку и сравниваем её `request_fingerprint`.

`request_fingerprint` — SHA-256 от канонизированного тела: ключи объекта отсортированы, применены документированные
defaults (`description`, `metadata`), `amount` нормализован как decimal (эквивалентные написания дают одинаковый
отпечаток), лишние пробелы не влияют. Совпадение отпечатка → `202` с сохранёнными `payment_id`/`created_at` и текущим
`status`; несовпадение → `409`.

Гонка разрешается самим ограничением БД, а не `SELECT` перед `INSERT`. Хранение отпечатка выбирается вместо
повторного сравнения полей: это детерминированно и не зависит от порядка ключей JSON.

### D3. Модель данных, ограничения и индексы

`payments`:

- `id` UUID PK (server default `gen_random_uuid()`).
- `idempotency_key` TEXT NOT NULL, `UNIQUE`.
- `request_fingerprint` CHAR(64) NOT NULL.
- `amount` NUMERIC(18,2) NOT NULL, `CHECK (amount > 0 AND amount <= 999999999.99)`; > 0 и ≤ 2 знаков, без округления.
- `currency` TEXT NOT NULL, `CHECK (currency IN ('RUB','USD','EUR'))`.
- `description` TEXT NOT NULL DEFAULT `''`; максимум 500 Unicode code points (`CHECK (char_length(description) <= 500)`
  как backstop, точную проверку делает Pydantic).
- `metadata` JSONB NOT NULL DEFAULT `'{}'`. **Python-атрибут НЕ может называться `metadata`**: на declarative-классе
  `metadata` зарезервирован SQLAlchemy. Правильный маппинг: `metadata_: Mapped[dict[str, Any]] =
  mapped_column("metadata", JSONB, default=dict)` — Python-атрибут `metadata_`, SQL-колонка `metadata`, внешнее поле
  API остаётся `metadata` (Pydantic alias/сериализация). Единственная семантика лимита: **≤ 16384 байта (16 KiB) UTF-8
  компактного канонического JSON**, измеряется в приложении как
  `len(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))`. DB-`CHECK` по
  `metadata::text` не добавляется (текстовое представление JSONB неэквивалентно компактному каноническому); авторитетная
  проверка — приложенческая, на входе.
- `status` TEXT NOT NULL, `CHECK (status IN ('pending','succeeded','failed'))`.
- `webhook_url` TEXT NOT NULL.
- `created_at` TIMESTAMPTZ NOT NULL DEFAULT `now()`, `processed_at` TIMESTAMPTZ NULL.
- Delivery state: `webhook_event_id` UUID NULL, `webhook_status` TEXT NOT NULL DEFAULT `'pending'`
  (`CHECK IN ('pending','delivered','failed')`), `webhook_attempts` INTEGER NOT NULL DEFAULT 0,
  `webhook_last_error` TEXT NULL.
- Индексы: `UNIQUE (idempotency_key)`; `INDEX (status)` частичный `WHERE status = 'pending'` для поиска зависших;
  `INDEX (created_at)`.

`outbox`:

- `id` UUID PK, `payment_id` UUID NOT NULL REFERENCES `payments(id)`, `event_type` TEXT NOT NULL,
  `payload` JSONB NOT NULL, `created_at` TIMESTAMPTZ NOT NULL DEFAULT `now()`.
- **Учёт публикаций relay** (отдельно от попыток consumer): `claimed_at TIMESTAMPTZ NULL` (лиз публикации),
  `published_at TIMESTAMPTZ NULL` (после publisher confirm). Записи не удаляются после публикации — остаются
  помеченными (D6).
- **Учёт попыток обработки consumer**: `attempt_no INTEGER NOT NULL DEFAULT 0` (номер текущей/последней попытки),
  `next_attempt_no INTEGER NULL` (номер, назначенный запланированному retry), `attempt_state TEXT NOT NULL DEFAULT
  'idle'` (`CHECK IN ('idle','open','awaiting_retry','completed')`), `attempt_owner TEXT NULL`, `attempt_epoch INTEGER
  NOT NULL DEFAULT 0` (fencing token), `attempt_lease_expires_at TIMESTAMPTZ NULL`, `attempt_heartbeat_at TIMESTAMPTZ
  NULL`, `next_attempt_at TIMESTAMPTZ NULL` (durable-расписание retry), `recoveries INTEGER NOT NULL DEFAULT 0`,
  `last_error TEXT NULL`, `last_attempt_at TIMESTAMPTZ NULL` (D8). Поля публикации и поля попыток не смешиваются.
  Частичный индекс `INDEX (next_attempt_at) WHERE attempt_state = 'awaiting_retry'` для retry dispatcher.
- Индексы: `INDEX (published_at) WHERE published_at IS NULL` (partial), `INDEX (created_at)`,
  `UNIQUE (payment_id, event_type)` — одно исходное событие на платёж.

Зафиксированные проектные решения (закрытые defaults, не открытые вопросы): `amount` > 0 и ≤ `999999999.99`
(NUMERIC(18,2), ≤ 2 знаков, без молчаливого округления); `description` — необязательное, default `''`, ≤ 500 code
points; `metadata` — необязательное, default `{}`, ≤ 16384 байта UTF-8 компактного канонического JSON. `amount` в
JSON — строка; `Decimal` сериализуется строковым сериализатором.

### D4. Машина состояний

`pending → succeeded` либо `pending → failed`. Оба терминальны и неизменяемы. `processed_at` устанавливается только
вместе с терминальным статусом. Переход выполняется одним условным апдейтом
`UPDATE payments SET status=..., processed_at=... WHERE id=:id AND status='pending' RETURNING id`, что даёт ровно
одного победителя при конкурентной обработке. Дополнительных бизнес-статусов нет; состояние webhook хранится отдельно
и не влияет на бизнес-статус.

### D5. HTTP-слой, OpenAPI и защита служебных маршрутов

FastAPI, `pydantic-settings` для конфигурации. Аутентификация — dependency, сравнивающая `X-API-Key` через
`secrets.compare_digest`. Служебные маршруты FastAPI (`/docs`, `/redoc`, `/openapi.json`) задаются как `docs_url=None,
redoc_url=None, openapi_url=None`; вместо них публикуется собственный `/api/v1/openapi.json`, защищённый той же
dependency. Так контракт остаётся доступным авторизованному ревьюеру и не открыт анонимно. Сервис не предоставляет
ни одного HTTP-маршрута, читаемого без валидного ключа: единственный health-маршрут **тоже защищён `X-API-Key`** и
вызывается из контейнера, поэтому анонимных HTTP-маршрутов нет. Healthcheck обязан проверять **работающий процесс**,
а не отдельный процесс, который лишь импортирует код: для `api` — живой ASGI-сервер и доступность БД (защищённый
`GET /api/v1/health`); для `consumer` — живые задачи обработчика и relay и доступность БД и брокера. Отдельная
команда «импорт + `SELECT 1`» как readiness не используется.

Альтернатива — оставить `/docs` открытыми — отклонена: нарушает требование «все эндпоинты». Альтернатива — полностью
удалить OpenAPI — отклонена: контракт должен быть проверяемым.

### D6. Outbox relay: claim, publisher confirm, границы транзакций

Relay — фоновая задача внутри процесса `consumer` (пятый обязательный сервис не вводится). Цикл:

1. Короткая транзакция: `SELECT ... FROM outbox WHERE published_at IS NULL
   AND (claimed_at IS NULL OR claimed_at < now() - :lease) ORDER BY created_at LIMIT :batch FOR UPDATE SKIP LOCKED`,
   проставить `claimed_at = now()`, закоммитить. Фильтр по `claimed_at` обязателен: без него после commit второй relay
   забрал бы ту же запись. `SKIP LOCKED` + лиз `claimed_at` с таймаутом не дают двум relay публиковать одну запись и
   позволяют восстановиться после падения.
2. Публикация **вне** DB-транзакции через publisher confirm с `mandatory=True`, чтобы поймать отсутствие маршрутизации.
3. Короткая транзакция: проставить `published_at = now()` только для подтверждённых сообщений.

Граница «брокер подтвердил → процесс упал до отметки» даёт повторную публикацию. Гарантия — at-least-once с
идемпотентной обработкой на стороне consumer, а не недоказанное exactly-once. Сбой публикации не удаляет outbox-запись:
она остаётся `published_at IS NULL` и будет переопубликована. Успешно опубликованные записи не удаляются, а остаются
помеченными (`published_at`), чтобы устойчивый учёт попыток consumer имел стабильный anchor (D8). Поля публикации
(`claimed_at`, `published_at`) используются только relay-ем и не участвуют в подсчёте попыток обработки. Лимит трёх
попыток обработки сообщения относится к consumer и не влияет на retention outbox, поэтому долгая недоступность
брокера не приводит к молчаливой потере событий.

Альтернатива — держать DB-транзакцию открытой во время публикации — отклонена: долгие транзакции и удержание блокировок
во время сетевых операций. Альтернатива — exactly-once через транзакционный publisher confirm в рамках одной транзакции —
недоступна для RabbitMQ.

### D7. Топология RabbitMQ и планирование retry

**Retry НЕ строится на TTL→DLX.** Документация RabbitMQ указывает, что dead-lettered сообщения по умолчанию
переопубликовываются **без publisher confirms** и могут быть потеряны, если целевая очередь не примет сообщение;
подтверждение публикации в retry-очередь не защищает последующую внутреннюю передачу брокера. Поэтому задержка retry
хранится в БД (`outbox.next_attempt_at`), а сообщение публикует **retry dispatcher** с publisher confirm (D8) — потеря
на TTL→DLX исключена структурно, retry-очереди с TTL и плагин `rabbitmq_delayed_message_exchange` не используются.

Топология (встроенные средства, без плагинов):

- `exchange payments` — direct, durable. Publisher публикует persistent-сообщения с routing key `payments.new`.
- `queue payments.new` — **quorum**, durable, bound к `payments` по ключу `payments.new`.
- `exchange payments.dlx` — direct, durable.
- `queue payments.new.dlq` — **quorum**, durable, bound к `payments.dlx` по ключу `payments.new`.

Quorum-очереди выбраны осознанно: по документации RabbitMQ они дают **at-least-once dead-lettering** (в отличие от
классических очередей, где dead-lettering по умолчанию без publisher confirms и может терять сообщения). Компромисс —
больший расход ресурсов и задержки при dead-lettering; для тестового решения приемлемо.

Сообщение несёт `event_id` исходного outbox-события для привязки к строке outbox, заголовок `x-attempt` (номер
попытки, которой адресовано сообщение) и, при необходимости, маркер `x-redelivery`. Задержки 1 с и 2 с соответствуют
двум паузам между тремя попытками и задаются конфигурацией. Топология объявляется идемпотентно как publisher-ом, так
и consumer-ом.

### D8. Попытки обработки, захват с лизом, fencing и окна падения

**Разделение учёта.** Публикация — `claimed_at`/`published_at` (relay, D6). Попытки consumer — `attempt_no`,
`next_attempt_no`, `attempt_state`, `attempt_owner`, `attempt_epoch`, `attempt_lease_expires_at`,
`attempt_heartbeat_at`, `next_attempt_at`, `recoveries`, `last_error`, `last_attempt_at` (D3). Поля публикации и поля
попыток не смешиваются.

**Атомарность номера попытки (нет `attempt_no = 0`).** Номер увеличивается **той же инструкцией**, что и открытие
попытки, поэтому окна «между захватом и инкрементом» нет:
`UPDATE outbox SET attempt_no = attempt_no + 1, attempt_state='open', attempt_epoch = attempt_epoch + 1,
 attempt_owner=:me, attempt_lease_expires_at=now()+lease, attempt_heartbeat_at=now(), recoveries=0,
 last_attempt_at=now(), next_attempt_at=NULL
 WHERE event_id=:eid AND attempt_state IN ('idle','awaiting_retry') AND :message_attempt_no = <ожидаемый>
 RETURNING attempt_no, attempt_epoch`.
После успешного захвата `attempt_no >= 1`; recovery не может увидеть 0. Retry-очередей с суффиксом-номером нет,
поэтому `retry.0` невозможен; лимит проверяется как `attempt_no >= 3`.

**Точный порядок одного прохода.**
1. **Захват/новая попытка или takeover** — атомарный `UPDATE` выше (новая попытка, инкремент) либо takeover (без
   инкремента, `attempt_epoch++`).
2. **Продление владения** — heartbeat: `UPDATE ... SET attempt_lease_expires_at=now()+lease, attempt_heartbeat_at=now()
   WHERE event_id=:eid AND attempt_owner=:me AND attempt_epoch=:my_epoch`.
3. **Шлюз** — вне DB-транзакции, только если платёж ещё `pending`.
4. **Сохранение результата** — отдельная короткая транзакция, условный терминальный апдейт
   `UPDATE payments SET status=..., processed_at=... WHERE id=:id AND status='pending' RETURNING id`, с fencing-проверкой
   владельца.
5. **Webhook** — HTTP вне DB-транзакции.
6. **Сохранение доставки** — короткая транзакция (`webhook_status`, `webhook_attempts`, `webhook_last_error`,
   `webhook_event_id`) с той же fencing-проверкой.
7. **ACK** — после шагов 4 и 6 (или после подтверждённой публикации в DLQ), затем закрытие попытки.
Захват не пишет `payments.status`; терминальный апдейт не выдаёт право на работу.

**Fencing — остановка прежнего владельца.** Каждый takeover увеличивает `attempt_epoch`. Все записи попытки
(heartbeat, терминал, доставка, закрытие) обусловлены `attempt_epoch = :my_epoch`; при несовпадении запись не проходит,
и устаревший обработчик обязан **немедленно прекратить работу**, не закрывать попытку и не ACK-ать как владелец. Так
медленный процесс не продолжит параллельно с новым и не создаст второй терминальный результат.

**Liveness и takeover по heartbeat.** Владелец продлевает лиз чаще, чем он истекает (`HEARTBEAT_INTERVAL ≪ lease`).
Takeover разрешён только при `attempt_state='open'` и `attempt_lease_expires_at < now()`.

**Активный чужой лиз — без потери работы.** Если доставка пришла, а лиз активен и heartbeat свежий (владелец жив),
consumer НЕ обрабатывает и НЕ увеличивает `attempt_no`; чтобы не остаться без сообщения при падении владельца, он
**сначала durable-публикует копию** (publisher confirm, `mandatory=True`, тот же `x-attempt`, маркер `x-redelivery`) и
**только затем ACK-ает** пришедшую доставку. Живой владелец → копия позже увидит `completed`/`open` и будет обработана
по правилам; упавший владелец → копия гарантирует recovery. Подтверждение доставки не уничтожает последнюю копию.

**`awaiting_retry`: законная следующая попытка vs старый дубль.** При планировании retry сохраняются `next_attempt_no`
и `next_attempt_at`; сообщение публикуется позже с `x-attempt = next_attempt_no`.
- `x-attempt == next_attempt_no` → **законная следующая попытка**: атомарный захват (инкремент до `next_attempt_no`,
  `attempt_epoch++`).
- `x-attempt < next_attempt_no` или `next_attempt_at > now()` → **старый дубль/слишком рано**: не обрабатывается,
  `attempt_no` не растёт, задержка не обходится; `ack` безопасен, т.к. durable-расписание retry хранится в БД.

**`completed`.** Повторная доставка → читается сохранённое состояние: терминал есть (шлюз не вызывается); webhook
доставлен → `ack`; иначе продолжается только доставка. Завершённая работа в DLQ не уходит.

**Планирование retry в БД (замена TTL→DLX).** При провале попытки одной транзакцией: `attempt_state='awaiting_retry'`,
`next_attempt_no = attempt_no + 1`, `next_attempt_at = now() + delay(attempt_no)`, `last_error`, освобождение
лиз/владельца; затем **`ack`** исходного — расписание уже durable в БД, подтверждённая публикация в брокер для retry не
нужна. Фоновая задача **retry dispatcher** (в процессе consumer) выбирает `attempt_state='awaiting_retry' AND
next_attempt_at <= now()` (`FOR UPDATE SKIP LOCKED`), публикует с `x-attempt = next_attempt_no`, **publisher confirm +
`mandatory`**, и только после confirm очищает `next_attempt_at`. Падение до очистки даёт повторную публикацию; дубль
безопасен и распознаётся по `x-attempt` при захвате. При `attempt_no >= 3` вместо retry публикуется в DLQ (confirm) и
попытка закрывается.

**Бюджет — по попыткам.** `attempt_no` растёт только при новой попытке; дубли и takeover бюджет не расходуют. Перед
проверкой бюджета consumer читает сохранённое состояние и не DLQ-ит завершённую работу. `recoveries` ограничен
`MAX_RECOVERIES`; сверх лимита попытка считается провалившейся и невыполненная работа уходит в DLQ.

**Стабильность эмулятора — не предотвращение вызова.** Детерминированность даёт тот же исход при легитимном повторе
(сид из `payment_id`, задержка из того же сида в [2;5]), но не предотвращает вызов; предотвращают захват (дубль) и
проверка терминального статуса (завершённый платёж). Для реального шлюза нужен его idempotency key.

Число внешних HTTP-вызовов не умножается: не более одного вызова шлюза и одной серии webhook-попыток на попытку,
вложенных retry-циклов нет.

### D9. Классификация ошибок

Бизнес-отказ шлюза (10%) — терминальный результат: сохраняется `failed`, вызывается webhook, повтор случайности не
происходит. Технические ошибки (исключение шлюза, ошибка БД, таймаут/сеть/`non-2xx` webhook) — повторяемые.
Ошибка webhook не меняет `succeeded` на `failed` и не перезапускает платёж: бизнес-результат уже сохранён, повторяется
только доставка.

### D10. Доставка webhook: состояние и стабильный event_id

Webhook-клиент — `httpx.AsyncClient` с таймаутом, `follow_redirects=False`; успех — только `2xx`. Состояние доставки
хранится в колонках `payments` (`webhook_status`, `webhook_attempts`, `webhook_last_error`, `webhook_event_id`), а не в
отдельной таблице — по YAGNI, при этом оно отделено от бизнес-`status`.

`event_id` — UUIDv5 с **фиксированным проектым namespace-константным UUID** (выделенный UUID проекта, зашитый в код,
а не случайное значение) и каноническим именем `webhook:<payment_id>:<status>`, где `payment_id` берётся в
каноническом lowercase hyphenated виде, а `status` — в lowercase. Значение вычисляется детерминированно и сохраняется
в `webhook_event_id` при первом терминальном переходе; при всех повторных отправках используется сохранённое значение,
поэтому один логический webhook всегда имеет один и тот же `event_id` независимо от числа отправок и рестартов. Это
позволяет получателю дедуплицировать.
Payload: `event_id`, `payment_id`, `status`, `processed_at`. Ограничение at-least-once: падение после успешного ответа
получателя, но до сохранения отметки, даёт повтор; это документируется в README.

### D11. Границы выбора механизмов

Захват/лиз и recovery покрывают конкурентные дубли и падения; TTL retry-очереди только планируют задержку;
детерминированный эмулятор стабилизирует легитимный повторный вызов, но не предотвращает его; предотвращение
повторного вызова — за захватом (дубль) и проверкой терминального статуса (завершённый платёж). Механизмы описаны
в D8 и не дублируются здесь.

### D12. Конфигурация, логирование, зависимости, инструменты

- Python 3.12; менеджер зависимостей — `uv` с `uv.lock` (проект новый, lock даёт воспроизводимость).
- `pydantic-settings` читает обязательные переменные (`API_KEY`, `DATABASE_URL`, `RABBITMQ_URL`); отсутствие приводит к
  быстрому падению на старте. `.env.example` без реальных секретов.
- Структурированное логирование с `payment_id`, `event_id`, номером попытки; API-ключ и полное содержимое произвольных
  метаданных не логируются.
- Ruff: `E`, `F`, `I`, `B`, `UP`, `ASYNC`, line-length 120; он же форматтер (единственный форматтер).
- mypy `strict = true` для приложения и тестов; точечные `overrides` для нетипизированных сторонних API (например,
  FastStream) вместо глобального `ignore_missing_imports`. `Any`/`cast` не используются как способ скрыть ошибки типов.

### D13. Стратегия тестирования

- `pytest` + `pytest-asyncio` в едином режиме `asyncio_mode = "auto"`; API — `httpx.AsyncClient` с `ASGITransport`;
  покрытие — `pytest-cov`.
- Недетерминизм убирается инъекцией: тестовые `RandomSource` (детерминированный/сидированный), `Clock`/`Sleeper`
  (мгновенное ожидание), эмулятор шлюза через `Protocol`. Тесты не ждут реальные 2–5 секунд и не падают из-за 10%.
- Unit: деньги/валюты/даты и их сериализация; границы `amount`/`description`/`metadata` (ровно 16384/16385 байт,
  500/501 code point) с измерением по UTF-8 компактного канонического JSON; нормализация и отпечаток; разрешённые
  переходы; обе ветви шлюза и границы задержки; стабильность результата эмулятора при легитимном повторе; retry по
  попыткам, а не доставкам (успех сразу, после 1–2 ошибок, исчерпание, падение на последней попытке); сопоставление
  `x-attempt` с ожидаемым номером (законная попытка vs старый дубль); fencing по `attempt_epoch`; бизнес-ошибка vs
  ошибка доставки; отсутствие повторного вызова шлюза при повторе webhook; классификация HTTP-ошибок и стабильность
  `event_id`.
- Integration (реальные PostgreSQL и RabbitMQ): API и коды ответов; rollback создания (нет ни платежа, ни outbox);
  идемпотентность (тот же/другой отпечаток); конкурентные POST → ровно один платёж и одно событие; фильтр `claimed_at`
  relay (нет двойной публикации); реальная публикация и восстановление при недоступности брокера; активный чужой лиз →
  durable-копия и отсутствие потери; takeover по heartbeat и fencing (старый владелец остановлен); различение
  конкурентного дубля, recovery и старого дубля в `awaiting_retry`; успешная третья попытка с падением до `ack` и
  redelivery; падение во время последней попытки; retry dispatcher с publisher confirm (расписание в БД, не TTL→DLX);
  DLQ только для незавершённой работы; сохранность состояния после рестарта; границы metadata на PostgreSQL (Unicode,
  несколько ключей, числовые значения); webhook `2xx`/`non-2xx`/таймаут и crash window после приёма; применение
  миграций к пустой PostgreSQL.
- E2E: POST → outbox → RabbitMQ → consumer → терминальный статус → локальный тестовый webhook-получатель → GET; отдельно
  сценарий исчерпания попыток и проверки DLQ. Тестовый receiver — fixture/опциональный test-profile, не обязательный
  продуктовый сервис.
- SQLite не заменяет PostgreSQL, моки брокера не заменяют реальную топологию. Обязательные интеграционные тесты не
  прячутся за постоянным `skip`; недоступная инфраструктура честно отмечается как непройденная проверка.

## Contracts

### HTTP API

- `POST /api/v1/payments` — заголовки `X-API-Key`, `Idempotency-Key`; тело
  `{"amount": "10.50", "currency": "RUB", "description": "...", "metadata": {}, "webhook_url": "http://..."}`;
  ответы: `202` (`payment_id`, `status`, `created_at`), `401`, `409`, `422`.
- `GET /api/v1/payments/{payment_id}` — заголовок `X-API-Key`; ответы: `200` (полное представление), `401`, `404`, `422`.
- `GET /api/v1/openapi.json` — заголовок `X-API-Key`; сгенерированный контракт.
- `GET /api/v1/health` — заголовок `X-API-Key`; проверка работающего процесса `api` (ASGI-сервер отвечает, БД доступна).
  Публичных анонимных HTTP-маршрутов нет.
- Обязательные лимиты контракта: `amount` ≤ `999999999.99` и ≤ 2 знаков; `description` ≤ 500 code points,
  default `''`; `metadata` ≤ 16384 байта UTF-8 компактного канонического JSON, default `{}`.
- `amount` — строка; `processed_at` — `null` до терминального статуса; все таймстампы UTC с tz.

### Message event (`payments.new`)

`{"event_id": "<uuid>", "event_type": "payment.created", "payment_id": "<uuid>", "occurred_at": "<utc>"}` плюс
заголовок `x-attempt` (номер попытки, которой адресовано сообщение; 1 при первой доставке) и, при необходимости,
маркер `x-redelivery`. Persistent, доставляется в durable quorum-очередь `payments.new`. `event_id` привязывает
сообщение к строке outbox, чей учёт попыток обработки — источник истины (D8); `x-attempt` должен совпасть с ожидаемым
номером, иначе сообщение считается дублем/устаревшим и не обрабатывается.

### Webhook payload

`POST` JSON `{"event_id": "<uuid>", "payment_id": "<uuid>", "status": "succeeded|failed", "processed_at": "<utc>"}`
с `Content-Type: application/json`. `event_id` — UUIDv5 от фиксированного namespace и канонического имени
`webhook:<payment_id>:<status>` (D10), стабилен для всех повторных отправок одного логического webhook. Успех — `2xx`;
таймаут задаётся конфигурацией; redirects не следуются.

## Failure Windows

| # | Окно | Поведение | Гарантия |
|---|------|-----------|----------|
| 1 | Коммит создания → брокер недоступен | `202` выдан, outbox-событие сохранено, опубликуется позже | Платёж не потерян |
| 2 | Relay: confirm получен → падение до отметки | Событие опубликуется повторно | at-least-once публикации; учёт публикаций отделён |
| 3 | Relay: две параллельные итерации | Фильтр `claimed_at` + `SKIP LOCKED` → запись берётся один раз | Нет двойной публикации одной записи |
| 4 | Consumer: падение между захватом и работой | Номер инкрементирован атомарно с захватом; recovery той же попытки, `attempt_no >= 1` | Нет `attempt_no = 0`, нет `retry.0` |
| 5 | Consumer: активный чужой лиз, heartbeat свежий | Durable-копия публикуется (confirm), затем `ack` исходной доставки | Работа не теряется, попытка не расходуется |
| 6 | Consumer: heartbeat устарел / лиз истёк | Takeover с `attempt_epoch++`; старый владелец остановлен fencing-ом | Нет двух действующих владельцев |
| 7 | Consumer: шлюз вызван → падение до сохранения | Recovery той же попытки; стабильный исход эмулятора; условный терминальный апдейт | Один стабильный результат |
| 8 | `awaiting_retry`: законная попытка vs старый дубль | `x-attempt == next_attempt_no` → попытка; иначе дубль без обработки | Задержка не обходится, бюджет не растёт |
| 9 | Retry dispatcher: падение после confirm | Повторная публикация с тем же `x-attempt` распознаётся при захвате | at-least-once, без двойной обработки |
| 10 | Retry: TTL→DLX не используется | Расписание в БД + dispatcher с publisher confirm | Потеря на внутреннем dead-lettering исключена |
| 11 | Терминальный статус сохранён → падение до webhook | Recovery продолжает доставку; шлюз не вызывается | Бизнес-статус неизменен |
| 12 | Webhook принят получателем → падение до отметки | Возможен повтор webhook | at-least-once, дедуп по стабильному `event_id` |
| 13 | Бюджет исчерпан / `recoveries` превышен | Невыполненная работа в DLQ; завершённая не DLQ-ится | Не теряется: DLQ + README-процедура разбора |

## Traceability Matrix

| Требование (spec) | Сценарий | Задача | Планируемый тест |
|---|---|---|---|
| payment-api: Authenticated API access | без/с неверным ключом → 401; docs/OpenAPI защищены; health тоже под ключом; нет анонимных маршрутов | T4.2, T4.4, T4.6, T10.6 | `tests/integration/test_api_auth.py` |
| payment-api: Payment creation endpoint | валидный POST → 202 pending; ответ после commit | T4.1, T5.3, T8.2 | `tests/integration/test_payments_api.py` |
| payment-api: Payment input validation | 422 money/currency/webhook/key; границы description 500/501 и metadata 16384/16385 | T4.1, T4.5, T7.1, T7.7, T8.12 | `tests/unit/test_money_validation.py`, `tests/integration/test_api_metadata_limits.py` |
| payment-api: Payment read endpoint | 200/404/422; `processed_at=null` | T4.3, T8.2 | `tests/integration/test_payments_api.py` |
| payment-api: Idempotent payment creation | дубль 202; конфликт 409; конкурентно 1 запись | T5.4, T8.4, T8.5 | `tests/integration/test_idempotency.py` |
| payment-api: OpenAPI contract fidelity | схема совпадает с поведением; health описан | T4.4 | `tests/integration/test_openapi_contract.py` |
| payment-processing: Atomic acceptance | совместный commit; rollback без следов | T5.3, T8.3 | `tests/integration/test_create_atomicity.py` |
| payment-processing: Durable Outbox publication | confirm→mark; сбой сохраняет; outage не теряет; запись retained; фильтр `claimed_at` (нет двойной публикации); учёт публикаций отделён | T6.1, T6.3, T8.6, T8.7 | `tests/integration/test_outbox_relay.py` |
| payment-processing: Single consumer orchestration | порядок claim→attempt→gateway→persist→webhook→persist→ack; терминал не зовёт шлюз | T6.4 | `tests/unit/test_consumer_orchestration.py`, `tests/e2e/test_payment_flow.py` |
| payment-processing: Claim separated from terminal status | захват не пишет статус; терминальный апдейт не выдаёт право; разные транзакции | T6.4, T6.5 | `tests/unit/test_consumer_orchestration.py` |
| payment-processing: Heartbeat, fencing, takeover | takeover по heartbeat; `attempt_epoch++`; старый владелец остановлен; активный чужой лиз → durable-копия до `ack` (нет потери) | T6.5, T6.10, T8.9 | `tests/integration/test_consumer_lease.py` |
| payment-processing: Emulated gateway behavior | успех; бизнес-отказ терминален; задержка 2–5 с; стабильность при легитимном повторе | T3.3, T7.3 | `tests/unit/test_gateway_emulator.py` |
| payment-processing: Payment status transitions | pending→succeeded/failed; терминал неизменяем | T3.4, T5.6, T7.2 | `tests/unit/test_status_transitions.py` |
| payment-processing: Duplicate vs recovery vs awaiting_retry | `x-attempt` различает законную попытку и дубль; задержка не обходится; дубли не расходуют бюджет | T6.5, T7.4, T8.9 | `tests/integration/test_retry_dlq.py` |
| payment-processing: Bounded retry (по попыткам) & safe scheduling | `attempt_no` атомарно с захватом (нет `retry.0`); расписание в БД + dispatcher с confirm (нет TTL→DLX); успешная 3-я+crash→ack без DLQ | T6.5, T6.11, T7.4, T8.9 | `tests/unit/test_retry_policy.py`, `tests/integration/test_retry_dlq.py` |
| payment-processing: Dead letter queue & ACK | DLQ только для незавершённой работы; топология без плагинов; invalid msg; `ack` после подтверждённой публикации | T3.2, T6.2, T6.5, T6.7, T8.9, T9.2 | `tests/integration/test_retry_dlq.py`, `tests/e2e/test_dlq_flow.py` |
| payment-processing: Transaction & resource boundaries | только async I/O; чистый shutdown | T2.2, T5.5, T6.8, T7.9 | `tests/unit/test_async_boundaries.py` |
| webhook-delivery: Payload contract | все поля; UUIDv5 от фикс. namespace и имени `webhook:<payment_id>:<status>`; стабильность при повторах/рестарте | T3.5, T7.6 | `tests/unit/test_webhook_payload.py` |
| webhook-delivery: Success criteria & network policy | 2xx успех; non-2xx/таймаут retry; redirects off | T3.5, T7.6 | `tests/unit/test_webhook_client.py` |
| webhook-delivery: State separated from business status | сбой не меняет статус; resume; рестарт | T5.6, T6.6, T8.10 | `tests/integration/test_webhook_delivery.py` |
| webhook-delivery: At-least-once & duplicate window | повтор после crash; дедуп по `event_id` | T6.6, T8.10, T9.1 | `tests/integration/test_webhook_delivery.py`, `tests/e2e/test_payment_flow.py` |
| webhook-delivery: Failure handling | общий бюджет попыток; не DLQ завершённую работу; финал → DLQ | T6.5, T8.9 | `tests/integration/test_retry_dlq.py` |
| service-runtime: Environment configuration | fail fast; `.env.example` без секретов | T2.1, T2.5, T7.8 | `tests/unit/test_settings.py` |
| service-runtime: Structured logging | корреляция; без секретов | T2.3, T7.8 | `tests/unit/test_logging.py` |
| service-runtime: Containerized topology | 4 сервиса; имена; volumes; readiness проверяет работающий процесс | T10.1, T10.2, T10.6 | `make integration` / ручная проверка compose |
| service-runtime: Readiness verifies running process | api — key-protected health (сервер+БД); consumer — живые handler/relay + БД/брокер | T4.6, T6.9, T10.6 | `tests/integration/test_health.py` / ручная проверка compose |
| service-runtime: Reproducible migrations | схема с нуля; один шаг; без create_all | T5.2, T8.11, T10.3 | `tests/integration/test_migrations.py` |
| service-runtime: Quality gates & docs | `make check`; integration-команда; README | T1.3, T10.4, T10.5, T12.1 | ручная проверка `make check` + ревью README |

## Risks / Trade-offs

- [At-least-once даёт дубли] → consumer идемпотентен (условный апдейт, дедуп по `event_id`), ограничение документируется.
- [Recovery может зацикливаться при постоянном падении] → лиз с TTL и `MAX_RECOVERIES` ограничивают recovery; сверх
  лимита попытка уходит в retry/DLQ, сообщение не теряется.
- [Дубли не должны влиять на бюджет] → попытки считаются по `attempt_no`, который инкрементируется атомарно с
  захватом; дубли, takeover и старый дубль в `awaiting_retry` бюджет не расходуют и различаются по `x-attempt`;
  активный чужой лиз не ACK-ается без durable-копии, поэтому единственная копия работы не теряется.
- [TTL→DLX может терять сообщения] → retry планируется в БД и публикуется dispatcher-ом с publisher confirm;
  quorum-очереди дают at-least-once dead-lettering; TTL-очереди для retry не используются. Компромисс quorum —
  больший расход ресурсов и задержки при dead-lettering.
- [Медленный владелец продолжает после takeover] → fencing по `attempt_epoch`: записи старого владельца отвергаются,
  он обязан немедленно остановиться; второй терминальный результат невозможен.
- [Платёж может остаться `pending` при трёх технических неудачах] → сообщение гарантированно в DLQ, README описывает разбор
  и повторную публикацию; терпимо для тестового решения.
- [Эмулятор не доказывает exactly-once реального списания] → детерминированный исход эмулятора помечен как решение для
  эмулятора; для реального провайдера требуется его idempotency key (вне области).
- [Долгий broker outage удерживает события в outbox] → retention outbox не ограничен лимитом попыток consumer; рост
  таблицы ожидаем и наблюдаем, очистка после восстановления.
- [`FOR UPDATE SKIP LOCKED` + лиз добавляют сложность relay] → оправдано конкурентностью и восстановлением после падения;
  батч ограничен, транзакции короткие.
- [Хранение состояния webhook в колонках `payments`] → проще, чем отдельная таблица; при росте требований (несколько
  подписчиков) можно вынести, но сейчас это YAGNI.

## Migration Plan

Проект greenfield, миграции данных нет. Развёртывание: `docker compose up -d postgres rabbitmq` → подождать healthchecks →
`docker compose run --rm api alembic upgrade head` (единственный шаг миграций, не запускается каждым сервисом) →
`docker compose up -d api consumer`. Откат: `docker compose down` и при необходимости `alembic downgrade base`; данные
хранятся в volumes, поэтому для чистого состояния — `docker compose down -v`.

## Resolved Defaults

Ранее открытые вопросы закрыты и зафиксированы как проектные решения (см. D3): верхняя граница `amount` —
`999999999.99`; `description` — необязательное, default `''`, ≤ 500 Unicode code points; `metadata` — необязательное,
default `{}`, ≤ 16384 байта канонического JSON (измеряется по UTF-8 длине `sort_keys`-сериализации). Открытых вопросов
нет; будущие уточнения значений не меняют архитектуру и разбиение задач.

## Purpose

Exposes authenticated HTTP endpoints to create and retrieve payments, so that API clients can submit a payment
once and safely retry the same request without creating duplicates, and can inspect the payment's current state.

## ADDED Requirements

### Requirement: Authenticated API access

Every HTTP endpoint SHALL require a static API key supplied in the `X-API-Key` request header. Requests with a
missing or incorrect key MUST be rejected with `401 Unauthorized` before any business logic runs. This MUST apply
to every HTTP route the service exposes, including the versioned API routes and the introspection routes
(`/docs`, `/redoc`, `/openapi.json`), which SHALL be either protected by the same key or disabled entirely. The
service MUST NOT expose any HTTP endpoint that can be read without a valid API key: a health endpoint, if provided,
MUST also require the key. Readiness MUST reflect a working process (the server is serving and its database is
reachable), not merely a process that imports the code, and MUST NOT reveal payment data.

#### Scenario: Request without API key is rejected

- **WHEN** a client calls any endpoint without the `X-API-Key` header
- **THEN** the service responds with `401 Unauthorized` and does not read or write payment data

#### Scenario: Request with an incorrect API key is rejected

- **WHEN** a client calls any protected endpoint with an `X-API-Key` value that does not match the configured key
- **THEN** the service responds with `401 Unauthorized`

#### Scenario: Introspection routes are not publicly readable

- **WHEN** a client requests `/docs`, `/redoc`, or `/openapi.json` without a valid `X-API-Key`
- **THEN** the service either responds `401 Unauthorized` or the route is not available at all

#### Scenario: No unauthenticated HTTP endpoint exists

- **WHEN** any HTTP route exposed by the service is called without a valid `X-API-Key`
- **THEN** the service responds `401 Unauthorized`; the health endpoint is key-protected and readiness reflects a working process

#### Scenario: Health endpoint requires the API key

- **WHEN** the key-protected health endpoint is called without a valid `X-API-Key`
- **THEN** the service responds `401 Unauthorized` and does not report readiness

### Requirement: Payment creation endpoint

The service SHALL expose `POST /api/v1/payments`. The request MUST carry an `Idempotency-Key` header and a JSON
body containing `amount`, `currency`, `description`, `metadata`, and `webhook_url`. On successful acceptance the
service MUST respond `202 Accepted` with a body containing `payment_id`, `status`, and `created_at`. The returned
`status` MUST reflect the payment's state at the time of the response.

#### Scenario: Valid request creates a pending payment

- **WHEN** a client sends a valid `POST /api/v1/payments` with a unique `Idempotency-Key` and a well-formed body
- **THEN** the service responds `202 Accepted` with `payment_id`, `status` equal to `pending`, and `created_at`

#### Scenario: Response is returned only after the payment is durably accepted

- **WHEN** the service accepts a valid creation request
- **THEN** the payment row and its initial outbox event are already committed before the `202 Accepted` response is sent

### Requirement: Payment input validation

The service SHALL validate the creation request and reject invalid input with `422 Unprocessable Entity`. The
`amount` MUST be a JSON string representing a decimal number greater than zero, with at most two decimal places and
a maximum value of `999999999.99`; it MUST NOT be silently rounded or truncated. The `currency` MUST be one of
`RUB`, `USD`, `EUR`. The `description` MUST be an optional string defaulting to the empty string and MUST NOT
exceed 500 Unicode code points. The `metadata` MUST be an optional JSON object defaulting to an empty object, and
its canonical JSON serialization MUST NOT exceed 16384 bytes (16 KiB), measured as the UTF-8 byte length of the
deterministically serialized object (`sort_keys=True`, compact separators, `ensure_ascii=False`); the service MUST
reject larger input rather than truncating it. The `webhook_url` MUST be a required absolute URL using the `http`
or `https` scheme. A missing or empty `Idempotency-Key` header MUST also be rejected with `422`.

#### Scenario: Non-positive amount is rejected

- **WHEN** a client submits an `amount` of `"0"` or a negative value
- **THEN** the service responds `422 Unprocessable Entity` and creates no payment

#### Scenario: Amount with too many decimal places is rejected

- **WHEN** a client submits an `amount` with three or more decimal places
- **THEN** the service responds `422 Unprocessable Entity` instead of rounding the value

#### Scenario: Amount above the documented bound is rejected

- **WHEN** a client submits an `amount` greater than `999999999.99`
- **THEN** the service responds `422 Unprocessable Entity`

#### Scenario: Unsupported currency is rejected

- **WHEN** a client submits a `currency` value outside `RUB`, `USD`, `EUR`
- **THEN** the service responds `422 Unprocessable Entity`

#### Scenario: Invalid webhook URL is rejected

- **WHEN** a client submits a `webhook_url` that is not an absolute `http` or `https` URL
- **THEN** the service responds `422 Unprocessable Entity`

#### Scenario: Missing idempotency key is rejected

- **WHEN** a client sends `POST /api/v1/payments` without the `Idempotency-Key` header
- **THEN** the service responds `422 Unprocessable Entity` and creates no payment

#### Scenario: Omitted optional fields receive documented defaults

- **WHEN** a client omits `description` and `metadata` from an otherwise valid request
- **THEN** the service accepts the request using the documented default values `''` and `{}` for those fields

#### Scenario: Description above the length limit is rejected

- **WHEN** a client submits a `description` longer than 500 Unicode code points
- **THEN** the service responds `422 Unprocessable Entity`

#### Scenario: Metadata exactly at the size limit is accepted

- **WHEN** a client submits `metadata` whose canonical JSON serialization is exactly 16384 bytes
- **THEN** the service accepts the request

#### Scenario: Metadata above the size limit is rejected

- **WHEN** a client submits `metadata` whose canonical JSON serialization exceeds 16384 bytes
- **THEN** the service responds `422 Unprocessable Entity` and creates no payment

### Requirement: Payment read endpoint

The service SHALL expose `GET /api/v1/payments/{payment_id}` returning the full payment representation for a
known identifier and `404 Not Found` for an unknown identifier. The representation MUST include `payment_id`,
`amount`, `currency`, `description`, `metadata`, `status`, `webhook_url`, `created_at`, and `processed_at`. The
`amount` MUST be serialized as a JSON string preserving the stored value. `processed_at` MUST be `null` until the
payment reaches a terminal state. All timestamps MUST be UTC with timezone information. `status` MUST be one of
`pending`, `succeeded`, `failed`.

#### Scenario: Existing payment is returned

- **WHEN** a client calls `GET /api/v1/payments/{payment_id}` for a payment that exists
- **THEN** the service responds `200 OK` with the full payment representation including `processed_at`

#### Scenario: Unknown payment identifier is not found

- **WHEN** a client calls `GET /api/v1/payments/{payment_id}` with a syntactically valid UUID that does not exist
- **THEN** the service responds `404 Not Found`

#### Scenario: Malformed payment identifier is rejected

- **WHEN** a client calls `GET /api/v1/payments/{payment_id}` with a value that is not a valid UUID
- **THEN** the service responds `422 Unprocessable Entity`

#### Scenario: Pending payment has no processed timestamp

- **WHEN** a client reads a payment that is still `pending`
- **THEN** `processed_at` is `null` and `status` is `pending`

### Requirement: Idempotent payment creation

Payment creation MUST be idempotent with respect to the `Idempotency-Key`. Repeating a request with the same key
and the same normalized body MUST return `202 Accepted` with the same `payment_id` and `created_at` as the first
request and with the payment's current `status`. Repeating a request with the same key but a different normalized
body MUST be rejected with `409 Conflict`. Normalization MUST canonicalize the decimal `amount` (so equivalent
decimal spellings compare equal), apply documented defaults to omitted optional fields, and compare JSON objects
regardless of key order. Idempotency MUST be enforced by a database uniqueness constraint with correct handling of
the insert race, not by a `SELECT`-before-`INSERT` check alone. A repeated request MUST NOT create a second
`payments` row or a second initial outbox event.

#### Scenario: Identical repeated request is deduplicated

- **WHEN** a client repeats a creation request with the same `Idempotency-Key` and the same normalized body
- **THEN** the service responds `202 Accepted` with the same `payment_id` and `created_at`, and no new payment or outbox row is created

#### Scenario: Conflicting body for the same key is rejected

- **WHEN** a client repeats a creation request with the same `Idempotency-Key` but a different normalized body
- **THEN** the service responds `409 Conflict` and creates no new payment

#### Scenario: Equivalent decimal spellings are treated as the same body

- **WHEN** a client repeats a request with the same `Idempotency-Key` and an `amount` that differs only in decimal spelling after normalization
- **THEN** the service treats it as a duplicate and responds `202 Accepted` with the original `payment_id`

#### Scenario: Concurrent identical requests create exactly one payment

- **WHEN** two identical creation requests with the same `Idempotency-Key` arrive concurrently
- **THEN** exactly one `payments` row and exactly one initial outbox event exist, and both callers receive the same `payment_id`

### Requirement: OpenAPI contract fidelity

The HTTP contract SHALL be described by the application's generated OpenAPI schema, including the `X-API-Key`
security scheme, required headers, request and response bodies, status codes, and representative examples. The
schema MUST be generated from the application's request/response models rather than maintained as a diverging
hand-written copy.

#### Scenario: Generated schema matches served behavior

- **WHEN** the generated OpenAPI document is inspected
- **THEN** it declares the `X-API-Key` security scheme and the documented request/response shapes and status codes for both endpoints

#### Scenario: Documented status codes are actually produced

- **WHEN** the documented `202`, `200`, `401`, `404`, `409`, and `422` cases are exercised
- **THEN** the service returns the status codes declared in the generated schema

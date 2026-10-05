## Purpose

Notifies the client about the final payment outcome by delivering a well-defined HTTP webhook with a stable event
identifier, so that receivers can deduplicate at-least-once deliveries and correlate the notification with a payment.

## ADDED Requirements

### Requirement: Webhook payload contract

The service SHALL deliver the notification as an HTTP `POST` with a JSON body. The payload MUST contain a stable
`event_id` that is unique per webhook event and unchanged across redeliveries of the same event, the `payment_id`,
the final `status`, and the `processed_at` timestamp. The content type MUST be `application/json`. The `event_id`
MUST be computed as a UUIDv5 over a fixed, project-wide namespace constant and a canonical name derived from the
payment identifier and the terminal status (for example `webhook:<payment_id>:<status>`, with the identifier in
canonical lowercase hyphenated form and the status in lowercase), so that the outcome for one payment is
deterministic. The computed value MUST be persisted at the first terminal transition and reused for every later
delivery, so that one logical webhook always carries the same `event_id` regardless of how many times it is sent
or how many times the process restarts.

#### Scenario: Payload carries the documented fields

- **WHEN** a webhook is delivered for a completed payment
- **THEN** the JSON body contains `event_id`, `payment_id`, `status`, and `processed_at`

#### Scenario: Redelivery reuses the same event identifier

- **WHEN** the same payment outcome is delivered more than once
- **THEN** every delivery carries the same `event_id` so the receiver can deduplicate

#### Scenario: Deterministic event identifier for a terminal status

- **WHEN** the webhook event identifier is computed for the same payment identifier and terminal status more than once
- **THEN** the same UUIDv5 value is produced every time

#### Scenario: Persisted identifier is reused across a restart

- **WHEN** a webhook is redelivered after a process restart
- **THEN** the delivery uses the previously persisted `event_id` and does not compute a different one

### Requirement: Delivery success criteria and network policy

The webhook client MUST apply a bounded request timeout. A delivery MUST be considered successful only when the
receiver responds with a `2xx` status code. Network errors, timeouts, and non-`2xx` responses MUST be treated as
retryable failures. The client MUST NOT follow redirects automatically.

#### Scenario: Successful delivery

- **WHEN** the receiver responds with a `2xx` status code within the timeout
- **THEN** the delivery is considered successful and no further attempt is required for it

#### Scenario: Non-success response is retryable

- **WHEN** the receiver responds with a non-`2xx` status code
- **THEN** the delivery is treated as a retryable failure

#### Scenario: Timeout is retryable

- **WHEN** the receiver does not respond within the configured timeout
- **THEN** the delivery is treated as a retryable failure

#### Scenario: Redirects are not followed

- **WHEN** the receiver responds with a redirect status code
- **THEN** the client does not follow the redirect and treats the response as a non-success

### Requirement: Delivery state separated from business status

The result of the payment business decision MUST be persisted before the webhook is sent. A webhook delivery
failure MUST NOT change a `succeeded` payment to `failed` and MUST NOT restart the payment business processing.
The state of webhook delivery MUST be stored durably and separately from the business status. Additional service
columns for delivery state are permitted when their purpose is documented; a separate table MUST be introduced
only if actually required. When the business result is already persisted, subsequent attempts MUST continue
webhook delivery instead of re-running the gateway.

#### Scenario: Webhook failure does not alter business status

- **WHEN** the webhook delivery fails for a successfully completed payment
- **THEN** the stored business status remains `succeeded` and the gateway is not invoked again

#### Scenario: Delivery resumes from the persisted result

- **WHEN** a message is retried after the business result was persisted
- **THEN** the consumer continues webhook delivery using the stored result

#### Scenario: Delivery state survives restart

- **WHEN** the consumer restarts between webhook attempts
- **THEN** the persisted delivery state and attempt information are retained

### Requirement: At-least-once delivery with documented duplicate window

Webhook delivery guarantee MUST be at-least-once. If the process fails after the receiver accepted a webhook but
before the delivery mark was persisted, the webhook MAY be delivered again. This duplicate window MUST be
documented as a known limitation, and receivers MUST deduplicate by the stable `event_id`.

#### Scenario: Crash after receiver acceptance causes a duplicate

- **WHEN** the receiver accepts a webhook and the process crashes before persisting the delivery mark
- **THEN** the webhook MAY be delivered again with the same `event_id`

#### Scenario: Receiver deduplicates by event identifier

- **WHEN** a receiver observes two deliveries with the same `event_id`
- **THEN** it can treat them as the same logical notification

### Requirement: Webhook delivery failure handling

Webhook delivery failures MUST share the bounded processing-attempt budget of the message being processed, so that a
single processing chain performs at most three processing attempts rather than spawning nested retry loops or
counting duplicate deliveries. Before dead-lettering, the consumer MUST re-check the persisted state: when the
business result is persisted the gateway is not re-run, when the webhook was already delivered the message is
acknowledged, and only unfinished work may be dead-lettered. If the final processing attempt fails, the message MUST
be routed to the dead letter queue with the delivery failure reason while the payment's business status remains
persisted. A missing or unavailable receiver MUST NOT cause the payment to lose its already stored outcome.

#### Scenario: Webhook retried within the shared budget

- **WHEN** webhook delivery fails and the processing attempt budget is not yet exhausted
- **THEN** the delivery is retried on the next attempt without exceeding three attempts in total

#### Scenario: Final webhook failure dead-letters the message

- **WHEN** the third attempt fails during webhook delivery
- **THEN** the message is dead-lettered with the failure reason and the payment keeps its persisted business status

#### Scenario: Unreachable receiver keeps the stored outcome

- **WHEN** the webhook receiver is unreachable while a payment outcome is already persisted
- **THEN** the payment status remains unchanged and only delivery is retried or dead-lettered

#### Scenario: Completed work is not dead-lettered

- **WHEN** a message is delivered again after the business result and webhook delivery are already persisted
- **THEN** the delivery is acknowledged without re-running the gateway, without consuming a processing attempt, and without dead-lettering

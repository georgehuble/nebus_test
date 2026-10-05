## Purpose

Guarantees that every accepted payment is processed asynchronously to a terminal status through an emulated
external gateway, using a transactional Outbox for durable publication, bounded retries with persistent attempt
accounting, and a dead letter queue for messages that cannot be completed.

## ADDED Requirements

### Requirement: Atomic acceptance of payment and outbox event

Creating a payment MUST persist the `Payment` row with status `pending` and its initial Outbox event in a single
database transaction. The `202 Accepted` response MUST be sent only after that transaction commits. If the
transaction fails, neither the payment nor the outbox event MUST remain. A failure of the message broker MUST NOT
lose an already accepted payment.

#### Scenario: Payment and outbox event commit together

- **WHEN** a valid creation request is processed
- **THEN** exactly one `pending` payment row and exactly one initial outbox event referencing it are committed

#### Scenario: Failed creation leaves no partial state

- **WHEN** the creation transaction fails before commit
- **THEN** neither a payment row nor an outbox event exists for that request

#### Scenario: Broker outage does not lose an accepted payment

- **WHEN** the broker is unavailable while a valid creation request is accepted
- **THEN** the payment and its outbox event are still committed and the caller receives `202 Accepted`

### Requirement: Durable Outbox publication

An Outbox relay SHALL publish committed outbox events to the message broker and mark them as published only after
receiving a publisher confirm that indicates the message was routed to a queue. Publish events MUST be persistent
and published to a durable exchange/queue topology. A publish failure MUST NOT delete or discard the outbox event,
and a published record MUST be retained in a marked state rather than deleted so that durable state keeps a stable
anchor. Publication accounting (the relay claim and the published mark) MUST be separate from consumer
processing-attempt accounting and MUST NOT be used to count processing attempts. The relay MUST read only committed
outbox records, MUST work in bounded batches, MUST claim records so that concurrent relay instances do not publish
the same record simultaneously, and MUST resume after a relay crash. The delivery guarantee for publication MUST be
at-least-once: a crash after broker confirmation but before the database mark MAY cause the event to be published
again, and consumers MUST tolerate duplicate delivery. The message-processing attempt limit MUST NOT be used to
silently drop outbox events during a prolonged broker outage.

#### Scenario: Event published and marked after confirmation

- **WHEN** a committed outbox event is relayed and the broker confirms routing to a queue
- **THEN** the event is marked as published and will not be relayed again under normal operation

#### Scenario: Publish failure preserves the outbox event

- **WHEN** publication fails or no publisher confirm is received
- **THEN** the outbox event remains unpublished and is retried later rather than being deleted

#### Scenario: Prolonged broker outage does not drop events

- **WHEN** the broker is unavailable for a period that exceeds the message-processing attempt limit
- **THEN** outbox events are retained and published once the broker recovers

#### Scenario: Crash after confirmation causes a safe duplicate

- **WHEN** the broker confirms publication but the process crashes before the outbox mark is persisted
- **THEN** the event MAY be published a second time, and the consumer handles the duplicate without a second business side effect

#### Scenario: Published outbox record is retained

- **WHEN** an outbox event is published and marked
- **THEN** the record remains stored in a published state and is not deleted, keeping durable attempt accounting anchored

### Requirement: Single consumer orchestration

Exactly one consumer SHALL receive messages from the new-payment queue and orchestrate the full processing
scenario. The consumer MUST delegate these responsibilities to separate components rather than implementing them
inline, and MUST perform them in this order: claim work, start the attempt, call the gateway, persist the terminal
result, deliver the webhook, persist delivery state, then acknowledge. Claiming work and changing the terminal
status MUST be separate operations on separate state; the claim MUST NOT change the payment status, and the
terminal status MUST be written only by the separate conditional update. Delivering a message for a payment that
already reached a terminal state MUST NOT invoke the gateway again.

#### Scenario: Consumer completes the scenario

- **WHEN** a new-payment message is delivered
- **THEN** the consumer produces a terminal business result, persists it, and attempts webhook delivery

#### Scenario: Redelivery of a completed payment skips the gateway

- **WHEN** a message is delivered again for a payment whose terminal business result is already persisted
- **THEN** the gateway is not invoked again and processing continues from the persisted result

#### Scenario: Claim and terminal status are separate

- **WHEN** the consumer claims a message for processing
- **THEN** claiming does not change the payment status, and the terminal status is written only by the separate conditional update after the gateway step

#### Scenario: Acknowledgement follows result and delivery persistence

- **WHEN** the consumer finishes a pass successfully
- **THEN** it acknowledges the message only after the terminal business result and the webhook delivery state are both persisted

### Requirement: Emulated gateway behavior

The gateway emulator SHALL simulate processing lasting between two and five seconds and SHALL return a successful
result with approximately 90% probability and a business decline with approximately 10% probability. A business
decline MUST be treated as a completed, terminal `failed` payment and MUST be persisted and reported through the
webhook; the emulator MUST NOT re-roll randomness until it obtains a success. The outcome and the simulated
duration MUST be a deterministic function of the payment identifier, so that a legitimate re-invocation after an
indeterminate crash returns the same result; this determinism stabilizes the result but MUST NOT be treated as
preventing a repeated gateway invocation. The duration source, the randomness source, and the waiting mechanism
MUST be injectable so tests can control them.

#### Scenario: Successful emulation

- **WHEN** the emulator produces a successful result
- **THEN** the payment transitions to `succeeded` and `processed_at` is recorded

#### Scenario: Business decline is terminal

- **WHEN** the emulator produces a business decline
- **THEN** the payment transitions to `failed`, the result is persisted, and a webhook is delivered without re-running the emulator

#### Scenario: Emulated duration stays within bounds

- **WHEN** the emulator runs
- **THEN** the simulated processing duration is between two and five seconds inclusive

#### Scenario: Re-invocation returns the same result

- **WHEN** the emulator is invoked again for the same payment identifier after a crash
- **THEN** it returns the same outcome and duration as the previous invocation

### Requirement: Payment status transitions

A payment MUST be created with status `pending` and MAY transition only to `succeeded` or to `failed`. Terminal
statuses MUST NOT be changed afterwards, and `processed_at` MUST be set only when a terminal status is reached.
No additional business statuses MUST be introduced.

#### Scenario: Pending transitions to succeeded

- **WHEN** processing completes successfully
- **THEN** the status becomes `succeeded` and `processed_at` is set

#### Scenario: Pending transitions to failed

- **WHEN** processing confirms a business decline
- **THEN** the status becomes `failed` and `processed_at` is set

#### Scenario: Terminal status is immutable

- **WHEN** a message is reprocessed for a payment that is already `succeeded` or `failed`
- **THEN** the stored status and `processed_at` are left unchanged

### Requirement: Protection against concurrent processing

The consumer MUST claim work with a lease before processing, keeping the claim separate from the terminal status
change, and MUST renew the lease with a heartbeat while it works. A concurrent duplicate delivery MUST NOT process
the payment while another owner holds a live lease, and MUST NOT release the last copy of the work: before
acknowledging such a duplicate the consumer MUST first publish a durable copy with order confirmation, so work
survives if the owner later fails. When the lease has expired because the owner stopped renewing its heartbeat, the
next delivery MUST take over as recovery of an indeterminate result and continue the same processing attempt rather
than starting a new one. Every takeover MUST advance a fencing token, and every state write by an attempt MUST be
conditional on that token, so a slow former owner is stopped and cannot proceed in parallel or produce a second
terminal result. The terminal transition MUST remain a single conditional update guarded by the current `pending`
status. The chosen mechanism for the emulator MUST NOT claim exactly-once semantics for a real external charge.

#### Scenario: Active lease is not released by acknowledging a duplicate

- **WHEN** a duplicate delivery arrives while another live owner holds the lease
- **THEN** a durable copy is published with order confirmation before the duplicate is acknowledged, so work is not lost if the owner later fails, and the attempt count is unchanged

#### Scenario: Takeover fences the previous owner

- **WHEN** a delivery takes over an attempt whose lease expired
- **THEN** the fencing token is advanced, the previous owner's state writes are rejected, and it stops working without producing a second terminal result

#### Scenario: Recovery after a crash continues the same attempt

- **WHEN** a delivery arrives after the previous owner crashed and the lease expired
- **THEN** it recovers the same processing attempt, does not increase the attempt count, and produces one stable terminal result

#### Scenario: Failure between gateway call and persistence resolves stably

- **WHEN** the worker crashes after the gateway call but before persisting the terminal result
- **THEN** recovery yields one stable terminal result without requiring the external gateway to guarantee exactly-once

### Requirement: Bounded retry counted by processing attempts

Message processing MUST allow at most three processing attempts in total, including the first, and MUST NOT exceed
that budget. Attempts MUST be counted as processing attempts, not as message deliveries. The attempt number MUST be
increased atomically in the same statement that claims the attempt, so recovery can never observe an attempt number
of zero and no attempt-indexed retry destination can be addressed. A duplicate delivery MUST NOT consume an
additional attempt, MUST NOT bypass the configured retry delay, and MUST NOT send already completed work to the
dead letter queue. A delivery MUST be matched against the persisted attempt state by its attempt header: only a
delivery whose attempt number equals the expected next attempt may start processing, while a lower number or an
early delivery is a stale duplicate. Before checking whether the budget is exhausted, the consumer MUST first read
the persisted state and act on it: skip the gateway when a terminal result exists, acknowledge when webhook delivery
is already persisted, and preserve the scheduled delay when a retry is awaiting. Retry scheduling MUST be stored
durably in the database and MUST survive a restart; an in-memory-only delay loop MUST NOT be the retry mechanism,
and retry MUST NOT rely on broker TTL dead-lettering for delivery, because that path is not covered by publisher
confirms. Between attempts the system MUST apply exponentially increasing delays (for example one second, then two
seconds). Retries MUST NOT be implemented as nested loops that multiply the number of external calls beyond the
attempt budget.

#### Scenario: Successful first attempt

- **WHEN** the first processing attempt completes the scenario
- **THEN** no further attempts are scheduled

#### Scenario: Success after transient failures

- **WHEN** the first or second attempt fails with a retryable technical error and a later attempt succeeds
- **THEN** the scenario completes within the three-attempt budget

#### Scenario: Attempt budget is exhausted

- **WHEN** all three processing attempts fail with retryable technical errors
- **THEN** the message is dead-lettered and no fourth attempt is scheduled

#### Scenario: Attempt accounting survives restart

- **WHEN** the consumer restarts while a message is awaiting a retry
- **THEN** the recorded attempt count and the next attempt schedule are preserved and the budget is not reset

#### Scenario: Successful third attempt crashing before acknowledgement

- **WHEN** the third attempt succeeds but the process crashes before acknowledging the message
- **THEN** the redelivery reads the persisted state, acknowledges without re-running the gateway, and does not dead-letter the completed work

#### Scenario: Concurrent duplicate while an attempt is active

- **WHEN** a duplicate delivery arrives while an attempt is active under an unexpired lease
- **THEN** a durable copy is published with order confirmation before the duplicate is acknowledged, it does not consume an attempt, and it does not dead-letter

#### Scenario: Stale duplicate while a retry is awaiting

- **WHEN** an old duplicate delivery arrives while the attempt state is awaiting a retry
- **THEN** it is acknowledged without consuming an attempt and without bypassing the scheduled retry delay

#### Scenario: Crash during the last allowed attempt

- **WHEN** the process crashes during the third processing attempt before a result is persisted
- **THEN** recovery continues the same attempt within the budget, and if it cannot complete the message is dead-lettered without starting a fourth attempt or losing the message

#### Scenario: Only the matching attempt number is processed

- **WHEN** a delivery whose attempt number does not match the expected next attempt is received
- **THEN** it is treated as a stale duplicate, is not processed, does not raise the attempt count, and does not jump ahead of the scheduled delay

#### Scenario: Attempt number is never zero

- **WHEN** the process crashes immediately after claiming an attempt
- **THEN** the attempt number was already increased atomically with the claim, so recovery continues with an attempt number of at least one

#### Scenario: Retry scheduling does not rely on broker TTL dead-lettering

- **WHEN** a retry is scheduled
- **THEN** it is persisted in the database and published later by a dispatcher with order confirmation, so no message is lost on the broker's internal dead-lettering path

### Requirement: Dead letter queue and delivery acknowledgement

After the attempt budget is exhausted (the third processing attempt fails, or the attempt cannot be completed)
unfinished work MUST be routed to a dead letter queue together with identifiers, the number of attempts, and the
failure reason. Dead-lettering MUST apply only to unfinished work: before dead-lettering, the consumer MUST read
the persisted state so that completed work is never dead-lettered. Queues used for the main delivery and for the
dead letter queue MUST be durable and MUST provide at-least-once dead-lettering (quorum queues), because
classic-queue TTL dead-lettering is not covered by publisher confirms and can lose messages. The message broker
topology MUST function in the provided Compose environment without third-party plugins. The consumer MUST
acknowledge a completed message only after its work is done, MUST acknowledge a failed message after its retry has
been durably scheduled in the database, and MUST acknowledge a dead-lettered message only after the dead-letter
publication has been confirmed by the broker; if that confirmation is not received, the original message MUST NOT
be acknowledged and will be redelivered, so no message is lost. Infinite `nack` with requeue MUST NOT be used as
the retry mechanism. Invalid message payloads and messages referencing an unknown payment MUST be handled
deterministically.

#### Scenario: Message dead-lettered after final failure

- **WHEN** the third processing attempt fails
- **THEN** a message is present in the dead letter queue carrying identifiers, the attempt count, and the failure reason

#### Scenario: Completed work is never dead-lettered

- **WHEN** a message is processed again for work whose business result and webhook delivery are already persisted
- **THEN** it is acknowledged and is not routed to the dead letter queue

#### Scenario: Topology works without plugins

- **WHEN** the service stack runs in Compose
- **THEN** exchanges, queues, bindings, retry routing, and the dead letter queue operate using only the broker's built-in capabilities

#### Scenario: Invalid message does not block the queue

- **WHEN** a message with an invalid payload is delivered
- **THEN** it is handled deterministically and does not cause an infinite redelivery loop

#### Scenario: Unknown payment identifier is handled

- **WHEN** a message references a payment identifier that does not exist
- **THEN** the consumer handles the message deterministically without creating inconsistent state

#### Scenario: Failed message is acknowledged only after confirmed publication

- **WHEN** a retryable failure routes the message to a retry queue or to the dead letter queue
- **THEN** the original message is acknowledged only after the broker confirms the new publication

#### Scenario: Unconfirmed publication causes redelivery, not loss

- **WHEN** the application crashes between publishing to the retry or dead letter queue and acknowledging the original
- **THEN** the original message is redelivered and the durable attempt counter keeps the attempt budget bounded

### Requirement: Transaction and resource boundaries

The system MUST use asynchronous I/O for database and network operations and MUST NOT block the event loop with
synchronous sleeping, synchronous database calls, or synchronous HTTP clients. A single database session MUST NOT
be shared between concurrent tasks. Database transactions MUST be short and MUST NOT hold a transaction open
across external network calls unless that decision is deliberate, bounded, and documented. Relay and background
tasks MUST be started on application startup and stopped cleanly on shutdown. The relay SHOULD run as a background
task within the existing consumer process so that no additional mandatory service is required.

#### Scenario: No blocking calls in async paths

- **WHEN** the request, processing, and delivery paths execute
- **THEN** all database and network operations are asynchronous and no blocking sleep or synchronous client is used

#### Scenario: Background tasks stop cleanly

- **WHEN** the consumer application shuts down
- **THEN** its relay and processing background tasks are cancelled and awaited without leaving dangling resources

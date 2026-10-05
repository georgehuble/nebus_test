## Purpose

Defines how the service is configured, containerized, migrated, logged, and verified, so that a reviewer can
bring the whole stack up reproducibly and run the same quality gates that the project itself uses.

## ADDED Requirements

### Requirement: Environment-based configuration

All configuration, including the API key, database connection, and broker connection, MUST be supplied through
environment variables, with a committed `.env.example` that contains placeholders instead of real secrets. The
repository MUST NOT contain real API keys or credentials. The API key MUST be compared in a way that does not
leak the expected value through error messages.

#### Scenario: Missing configuration fails fast

- **WHEN** a required environment variable is absent or empty at startup
- **THEN** the application fails fast with a clear error instead of running with an insecure default

#### Scenario: Example environment contains no real secrets

- **WHEN** the committed `.env.example` is inspected
- **THEN** it contains only placeholder values and no real credentials

### Requirement: Structured logging without sensitive data

The service SHALL emit structured logs that include the `payment_id`, `event_id`, and attempt number where they
apply. Logs MUST NOT contain the configured API key and MUST NOT contain the full contents of arbitrary payment
metadata.

#### Scenario: Processing logs carry correlation identifiers

- **WHEN** a payment is processed and a webhook is delivered
- **THEN** the corresponding log records include `payment_id`, `event_id`, and the attempt number

#### Scenario: Sensitive data is not logged

- **WHEN** logs are inspected for a request that carried an API key and arbitrary metadata
- **THEN** neither the API key value nor the full arbitrary metadata contents appear in the logs

### Requirement: Containerized service topology

Docker Compose SHALL start four services: `postgres`, `rabbitmq`, `api`, and `consumer`. Services MUST reach each
other using Compose service names rather than `localhost`. Database and broker data MUST use persistent volumes.
Services that depend on `postgres` or `rabbitmq` MUST wait for readiness using healthchecks rather than fixed
sleeps. Readiness of `api` and `consumer` MUST be verified by a healthcheck that exercises the running process: for
`api`, a key-protected health endpoint that confirms the ASGI server is serving and its database is reachable; for
`consumer`, a check that confirms the running handler and relay tasks are alive and the database and broker
connections are healthy. A separate process that only imports the code and runs `SELECT 1` MUST NOT be treated as
sufficient readiness. Health output MUST NOT expose payment data. The stack MUST stop cleanly and MUST NOT require
any additional mandatory service.

#### Scenario: Stack starts with four services

- **WHEN** the Compose stack is started
- **THEN** `postgres`, `rabbitmq`, `api`, and `consumer` run and become healthy

#### Scenario: Inter-service connectivity uses service names

- **WHEN** the `api` or `consumer` service connects to the database or broker
- **THEN** it connects through the Compose service name and not through `localhost`

#### Scenario: Dependencies wait for readiness

- **WHEN** the `api` or `consumer` service starts while `postgres` or `rabbitmq` is still starting
- **THEN** it waits for the dependency healthcheck before beginning work

#### Scenario: Data survives container restarts

- **WHEN** the stack is restarted
- **THEN** previously stored database and broker data persist through volumes

#### Scenario: API readiness reflects a working process

- **WHEN** the `api` container healthcheck runs
- **THEN** it calls the key-protected health endpoint from inside the container and readiness is reported only when the server is serving and the database is reachable

#### Scenario: Consumer readiness reflects running tasks

- **WHEN** the `consumer` container healthcheck runs
- **THEN** readiness is reported only when the running handler and relay tasks are alive and the database and broker connections are healthy, not merely because a separate process could import the code

### Requirement: Reproducible database migrations

Database schema MUST be created by versioned migrations, and the application MUST NOT use an automatic
`create_all` substitute at startup. Applying migrations MUST be an unambiguous, reproducible step that runs once
rather than being attempted concurrently by every service.

#### Scenario: Migrations build the schema from empty database

- **WHEN** migrations are applied to an empty PostgreSQL instance
- **THEN** the `payments` and `outbox` schema including constraints and indexes is created

#### Scenario: Migrations are not run concurrently

- **WHEN** the stack starts
- **THEN** migration application happens in a single well-defined step and is not raced by multiple services

#### Scenario: Schema is not created implicitly

- **WHEN** the application starts against an unmigrated database
- **THEN** it does not silently create tables outside the migration mechanism

### Requirement: Reproducible quality gates and documentation

The project SHALL provide a single command that runs Ruff lint, Ruff format check, mypy, and pytest, and a
separate documented command that brings up the required infrastructure and runs the integration and end-to-end
tests. The README MUST document environment setup, build and start commands, the migration step, curl examples for
both endpoints including the idempotent repeat and the conflict case, the webhook payload, how to inspect the dead
letter queue, the Outbox/retry scheme, and the documented guarantee limitations. The README MUST state that
`localhost` inside the consumer refers to the consumer container itself and MUST show a container-reachable address
for the test webhook receiver.

#### Scenario: Single combined check command

- **WHEN** the documented combined check command is executed
- **THEN** it runs Ruff lint, Ruff format check, mypy, and pytest

#### Scenario: Infrastructure-backed test command

- **WHEN** the documented integration command is executed
- **THEN** it starts the required PostgreSQL and RabbitMQ infrastructure and runs the integration and end-to-end tests

#### Scenario: README enables an independent reviewer

- **WHEN** a reviewer follows the README from a clean checkout
- **THEN** they can start the stack, apply migrations, call both endpoints with curl, and inspect the dead letter queue

#### Scenario: Container networking is documented

- **WHEN** a reviewer configures a webhook URL while reading the README
- **THEN** the README explains that `localhost` refers to the consumer container and provides a container-reachable receiver address

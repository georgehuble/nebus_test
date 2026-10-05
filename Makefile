# The default target keeps a bare `make` useful.
.DEFAULT_GOAL := help

# Host ports for the compose infrastructure (override if they clash locally).
POSTGRES_HOST_PORT ?= 15432
RABBITMQ_HOST_PORT ?= 15673
DATABASE_URL ?= postgresql+asyncpg://payments:payments@localhost:$(POSTGRES_HOST_PORT)/payments
RABBITMQ_URL ?= amqp://guest:guest@localhost:$(RABBITMQ_HOST_PORT)/
API_KEY ?= dev-api-key-change-me

.PHONY: help sync lint format-check format mypy unit check up down migrate integration coverage

help:
	@echo "Targets:"
	@echo "  sync        - install all dependencies (uv sync --all-groups)"
	@echo "  lint        - ruff check + ruff format --check"
	@echo "  mypy        - static type checking"
	@echo "  unit        - run unit tests"
	@echo "  check       - lint + mypy + unit tests (single quality gate)"
	@echo "  up          - start postgres and rabbitmq and wait for health"
	@echo "  migrate     - apply Alembic migrations (single migration step)"
	@echo "  integration - start infrastructure, migrate, run integration and e2e tests"
	@echo "  coverage    - run the whole suite with branch coverage"
	@echo "  down        - stop the stack and remove volumes"

sync:
	uv sync --all-groups

lint:
	uv run ruff check .
	uv run ruff format --check .

mypy:
	uv run mypy .

unit:
	uv run pytest -m unit

check: lint mypy unit

up:
	docker compose up -d --wait postgres rabbitmq

migrate:
	DATABASE_URL="$(DATABASE_URL)" uv run alembic upgrade head

integration: up
	DATABASE_URL="$(DATABASE_URL)" uv run alembic upgrade head
	DATABASE_URL="$(DATABASE_URL)" RABBITMQ_URL="$(RABBITMQ_URL)" API_KEY="$(API_KEY)" \
		uv run pytest -m "integration or e2e"

coverage:
	uv run pytest --cov=app --cov-report=term-missing

down:
	docker compose down -v

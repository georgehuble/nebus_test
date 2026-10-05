# Contributing

Thanks for taking the time to improve this project. This document describes the
expected local workflow, the quality gates and the conventions used in the
repository.

## Development environment

The service targets **Python 3.12** and uses [uv](https://docs.astral.sh/uv/) for
dependency management.

```bash
# 1. Install uv (see the official docs for other platforms)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Install all dependency groups (runtime + dev)
uv sync --all-groups

# 3. Enable the local git hooks (ruff, mypy, whitespace fixes)
uv run pre-commit install
```

Docker Engine 24+ and Docker Compose v2 are required to run the integration and
end-to-end suites, which depend on **real** PostgreSQL and RabbitMQ instances.

## Quality gates

Run the full local gate before opening a pull request:

```bash
# Lint, format check, strict typing and unit tests (single gate)
make check

# Integration + end-to-end tests on real infrastructure
make integration

# Branch coverage for the whole suite
make coverage
```

The CI pipeline runs the same commands, so a green local run is a strong signal
that CI will pass.

| Gate | Command | What it enforces |
|---|---|---|
| Lint | `uv run ruff check .` | Imports, bug-prone patterns, async pitfalls |
| Format | `uv run ruff format --check .` | Deterministic formatting |
| Types | `uv run mypy .` | `strict` mode for the `app` package |
| Unit | `uv run pytest -m unit` | Fast, infrastructure-free tests |
| Integration | `uv run pytest -m integration` | Behaviour against real PostgreSQL/RabbitMQ |
| E2E | `uv run pytest -m e2e` | Full-stack scenarios |

## Coding standards

- Follow the layering described in [`README.md`](README.md): `api` → `application`
  → `domain`, with `infrastructure` providing adapters behind narrow `Protocol`s.
- Keep I/O at the edges; business rules belong in `application`/`domain`.
- All public modules, classes and functions carry docstrings; keep them accurate.
- Never commit secrets. Use `.env` (git-ignored) and document new variables in
  [`.env.example`](.env.example) and the configuration table in the README.
- New behaviour requires tests: unit tests for pure logic, integration tests for
  persistence/messaging, and an e2e test when a cross-process flow changes.

## Commit conventions

This project follows [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <summary>
```

Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `ci`, `perf`,
`build`. Examples:

```
feat(processing): add takeover fencing epoch
fix(outbox): publish outside the claim transaction
docs(readme): document webhook event_id determinism
```

Keep commits focused and the subject in the imperative mood.

## Pull requests

1. Branch from `main` using a descriptive name (`feat/...`, `fix/...`, `docs/...`).
2. Make sure `make check` and `make integration` pass.
3. Update documentation, `.env.example` and [`CHANGELOG.md`](CHANGELOG.md) when
   behaviour or configuration changes.
4. Fill in the pull request template and link the related issue.
5. A maintainer reviews the change; CI must be green before merging.

## Reporting bugs and requesting features

Use the GitHub issue templates. For security-sensitive reports, follow
[`SECURITY.md`](SECURITY.md) instead of opening a public issue.

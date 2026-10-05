"""Fail-fast configuration behaviour."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from app.settings import Settings
from pydantic import ValidationError

pytestmark = pytest.mark.unit


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the required variables so tests control their presence."""
    for name in ("API_KEY", "DATABASE_URL", "RABBITMQ_URL"):
        monkeypatch.delenv(name, raising=False)


def test_missing_required_variable_fails_fast(clean_env: None) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    assert "api_key" in str(excinfo.value)


def test_empty_api_key_fails_fast(clean_env: None) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            api_key="",  # type: ignore[arg-type]
            database_url="postgresql+asyncpg://u:p@localhost:5432/db",
            rabbitmq_url="amqp://guest:guest@localhost:5672/",
        )
    assert "api_key" in str(excinfo.value)


def test_database_url_requires_asyncpg_scheme() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            api_key="k",  # type: ignore[arg-type]
            database_url="postgresql://u:p@localhost:5432/db",
            rabbitmq_url="amqp://guest:guest@localhost:5672/",
        )


def test_rabbitmq_url_requires_amqp_scheme() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            api_key="k",  # type: ignore[arg-type]
            database_url="postgresql+asyncpg://u:p@localhost:5432/db",
            rabbitmq_url="http://localhost:5672/",
        )


def test_retry_delay_is_exponential(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory(retry_base_delay_seconds=1.0)
    assert settings.retry_delay_seconds(1) == 1.0
    assert settings.retry_delay_seconds(2) == 2.0
    assert settings.retry_delay_seconds(3) == 4.0


def test_api_key_is_masked_in_repr(settings_factory: Callable[..., Settings]) -> None:
    settings = settings_factory(api_key="super-secret")
    assert "super-secret" not in repr(settings)
    assert settings.api_key.get_secret_value() == "super-secret"


def test_env_example_contains_only_placeholders() -> None:
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / ".env.example").read_text(encoding="utf-8")
    assert "API_KEY=replace-me-with-your-own-api-key" in text
    # No value should look like a high-entropy secret (32+ hex/base64 characters).
    for line in text.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        _, _, value = line.partition("=")
        assert not re.search(r"[A-Za-z0-9+/]{32,}", value), line

"""Structured logging correlation and sensitivity."""

from __future__ import annotations

import io
import json
import logging

import pytest
from app.logging_config import CORRELATION_FIELDS, JsonFormatter, get_logger

pytestmark = pytest.mark.unit


def _capture(logger_name: str) -> tuple[io.StringIO, logging.Logger]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(logger_name)
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return stream, logger


def test_correlation_fields_are_emitted() -> None:
    stream, logger = _capture("tests.logging.correlation")
    bound = get_logger("tests.logging.correlation").bind(
        payment_id="11111111-1111-1111-1111-111111111111",
        event_id="22222222-2222-2222-2222-222222222222",
        attempt_no=2,
    )
    bound.info("processing payment")
    payload = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert payload["payment_id"] == "11111111-1111-1111-1111-111111111111"
    assert payload["event_id"] == "22222222-2222-2222-2222-222222222222"
    assert payload["attempt_no"] == "2"
    assert payload["message"] == "processing payment"


def test_api_key_and_metadata_are_never_correlation_fields() -> None:
    assert "api_key" not in CORRELATION_FIELDS
    assert "metadata" not in CORRELATION_FIELDS


def test_secret_and_metadata_are_absent_from_output() -> None:
    stream, logger = _capture("tests.logging.secrets")
    api_key = "sk_live_do_not_log_me"
    metadata = {"pan": "4111111111111111", "cvv": "123"}
    assert "cvv" in metadata
    bound = get_logger("tests.logging.secrets").bind(payment_id="33333333-3333-3333-3333-333333333333")
    bound.info("payment accepted")
    output = stream.getvalue()
    assert api_key not in output
    assert "4111111111111111" not in output
    assert "cvv" not in output
    assert json.loads(output.strip().splitlines()[-1])["message"] == "payment accepted"
    assert logger is logging.getLogger("tests.logging.secrets")


def test_formatter_renders_single_line_json() -> None:
    stream, _ = _capture("tests.logging.json")
    get_logger("tests.logging.json").info("hello")
    lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["level"] == "INFO"

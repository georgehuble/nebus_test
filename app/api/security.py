"""Static API-key authentication for every HTTP route."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    request: Request,
    api_key: Annotated[str | None, Security(api_key_header)] = None,
) -> None:
    """Reject requests without a valid ``X-API-Key`` before any business logic."""
    expected: str | None = getattr(request.app.state, "api_key_value", None)
    if expected is None or api_key is None or not secrets.compare_digest(api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "APIKey"},
        )

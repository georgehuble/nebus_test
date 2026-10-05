"""FastAPI dependencies exposing the application container."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.container import ApiContainer


def get_container(request: Request) -> ApiContainer:
    """Return the container stored on the running application."""
    container: ApiContainer | None = getattr(request.app.state, "container", None)
    if container is None:  # pragma: no cover - only before lifespan startup
        msg = "application container is not initialised"
        raise RuntimeError(msg)
    return container


ContainerDep = Annotated[ApiContainer, Depends(get_container)]

"""Default implementations of the replaceable system protocols."""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime


class SystemClock:
    """Timezone-aware UTC clock based on the system wall clock."""

    def now(self) -> datetime:
        """Return the current UTC time."""
        return datetime.now(UTC)


class AsyncioSleeper:
    """Sleeper backed by :func:`asyncio.sleep` (never blocks the loop)."""

    async def sleep(self, seconds: float) -> None:
        """Yield control for ``seconds``."""
        await asyncio.sleep(seconds)


class DefaultRandomSource:
    """Unseeded pseudo-random source used by the gateway emulator by default."""

    def __init__(self) -> None:
        self._generator = random.Random()

    def random(self) -> float:
        """Return a value in ``[0.0, 1.0)``."""
        return self._generator.random()

    def seed(self, seed: int) -> None:
        """Reseed the generator deterministically."""
        self._generator.seed(seed)

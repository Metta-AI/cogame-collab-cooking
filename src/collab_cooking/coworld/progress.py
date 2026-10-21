"""One latest private snapshot per actual attempt and one owned wire send."""

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

from . import lifecycle
from .native import Attempt
from .plan_protocol import AttemptProgress


class ProgressWriter:
    def __init__(self, request_id: UUID, send: Callable[[AttemptProgress], Awaitable[None]]):
        self.request_id = request_id
        self.send = send
        self.latest: dict[str, Attempt] = {}
        self.known: set[str] = set()
        self.inflight: AttemptProgress | None = None
        self.changed = asyncio.Event()
        self.idle = asyncio.Event()
        self.idle.set()
        self.closed = False
        self.task = lifecycle.owned_task(self._write())

    async def update(self, attempt: Attempt) -> None:
        if self.closed:
            raise ValueError("private progress writer admission is closed")
        if attempt.attempt_id not in self.known and len(self.known) >= 2:
            raise ValueError("private progress exceeds source-owned retry budget")
        self.known.add(attempt.attempt_id)
        self.latest[attempt.attempt_id] = attempt.model_copy(deep=True)
        self.idle.clear()
        self.changed.set()

    async def _write(self) -> None:
        while True:
            await self.changed.wait()
            self.changed.clear()
            while self.latest:
                key = next(iter(self.latest))
                attempt = self.latest.pop(key)
                self.inflight = AttemptProgress(
                    window_id=self.request_id,
                    attempt_id=UUID(attempt.attempt_id),
                    evidence=attempt,
                )
                await self.send(self.inflight)
                self.inflight = None
            self.idle.set()
            if self.closed:
                return

    async def finish(self, deadline: float) -> bool:
        """Seal admission after native producers join, then drain under one deadline."""
        self.closed = True
        self.changed.set()
        if not await lifecycle.settle({self.task}, deadline, cancel=False):
            joined = await lifecycle.settle({self.task}, deadline, cancel=True)
            if joined and not self.task.cancelled():
                self.task.result()
            return joined and not self.latest and self.inflight is None
        self.task.result()
        return True

"""The container owns native inference; the game owns every grant and installation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
from pydantic import ValidationError

from . import lifecycle
from .native import Attempt, LearnerScope, ResponseRejected, complete
from .plan_protocol import AttemptProgress, PlanReply, RequestGrant
from .plans import PlanError, parse_plan
from .progress import ProgressWriter


class PlanWorker:
    """One admitted call and coalescing writer, settled before a reply or stop credit."""

    def __init__(
        self,
        slot: int,
        endpoint: str,
        send_progress: Callable[[AttemptProgress], Awaitable[None]],
    ):
        self.slot = slot
        self.endpoint = endpoint
        self.send_progress = send_progress
        self.task: asyncio.Task[PlanReply] | None = None
        self.writer: ProgressWriter | None = None
        self.attempt: Attempt | None = None
        self.stopping = False
        self.owner = lifecycle.Owner(parent=lifecycle.current_owner.get())

    def start(self, grant: RequestGrant) -> asyncio.Task[PlanReply]:
        if self.stopping or (self.task is not None and not self.task.done()):
            raise ValueError("native plan owner is not available")
        if grant.window.slot != self.slot:
            raise ValueError("native grant differs from authenticated player seat")
        if self.task is not None:
            error = self.task.exception()
            if error is not None:
                raise error
        self.task = lifecycle.owned_task(self._run(grant))
        return self.task

    async def _run(self, grant: RequestGrant) -> PlanReply:
        request = grant.window.native_request(grant.user)
        scope = LearnerScope(endpoint=self.endpoint, slot=self.slot, model=grant.window.profile.model)
        token = lifecycle.current_owner.set(self.owner)
        writer = ProgressWriter(grant.window.window_id, self.send_progress)
        self.writer = writer
        attempt = Attempt(
            attempt_id=str(grant.attempt_id),
            slot=self.slot,
            stage="plan",
            request=request,
            timeout_ms=grant.timeout_seconds * 1000,
        )
        self.attempt = attempt
        native_task = lifecycle.owned_task(
            complete(
                request,
                scope,
                stage="plan",
                timeout=grant.timeout_seconds,
                attempt=attempt,
                progress=writer.update,
            )
        )
        try:
            await asyncio.wait({native_task})
            error = native_task.exception()
            if isinstance(error, lifecycle.OwnershipUnsettled):
                raise error
            if error is not None and not isinstance(
                error, (httpx.HTTPError, TimeoutError, ResponseRejected, ValidationError)
            ):
                raise error
            response = None
            parsed = None
            rejection = type(error).__name__ if error is not None else None
            if error is None:
                completion = native_task.result().text
                response = completion

                async def parse():
                    return parse_plan(completion, grant.window.legal_stations, grant.window.ally_aliases)

                parser = lifecycle.owned_task(parse())
                await asyncio.wait({parser})
                parse_error = parser.exception()
                if isinstance(parse_error, PlanError):
                    rejection = parse_error.cause
                elif parse_error is not None:
                    raise parse_error
                else:
                    parsed = parser.result()
            if not await writer.finish(lifecycle.cleanup_deadline()):
                raise lifecycle.OwnershipUnsettled("private native plan writer did not drain")
            if attempt.response_reader_joined is False:
                raise lifecycle.OwnershipUnsettled("native plan reader did not join")
            return PlanReply(
                window_id=grant.window.window_id,
                attempt_id=grant.attempt_id,
                response=response,
                submitted_plan=parsed,
                rejection_reason=rejection,
                reader_joined=True,
            )
        finally:
            try:
                deadline = lifecycle.cleanup_deadline()
                native_joined = await lifecycle.settle({native_task}, deadline, cancel=True)
                if native_joined and not writer.closed:
                    if not await writer.finish(deadline):
                        raise lifecycle.OwnershipUnsettled("cancelled native plan writer did not drain")
                if not native_joined:
                    raise lifecycle.OwnershipUnsettled("cancelled native plan reader did not join")
            finally:
                lifecycle.current_owner.reset(token)

    async def stop(self, deadline: float) -> bool:
        """Only actual owned termination permits the caller to emit its joined ACK."""
        self.stopping = True
        self.owner.stopping = True
        self.owner.deadline = deadline if self.owner.deadline is None else min(deadline, self.owner.deadline)
        if self.task is None:
            return True
        joined = await lifecycle.settle({self.task}, deadline, cancel=True)
        if not joined:
            return False
        if not await lifecycle.settle_owner(self.owner, deadline, cancel=True):
            return False
        if not self.task.cancelled():
            error = self.task.exception()
            if isinstance(error, lifecycle.OwnershipUnsettled):
                return False
            if error is not None:
                raise error
        if self.attempt is not None and self.attempt.response_reader_joined is False:
            return False
        return self.writer is None or (
            self.writer.task.done() and not self.writer.latest and self.writer.inflight is None
        )

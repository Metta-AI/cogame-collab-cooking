"""Player-container inference and executor owners for the definitive native wire."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from . import lifecycle
from .macro_executor import MacroExecutor
from .native import LearnerScope
from .plan_player import PlanWorker
from .plan_protocol import (
    GAME_FRAME,
    DirectiveInstalled,
    EvidenceReceived,
    NativeRegistration,
    PhysicalAction,
    PhysicalObservation,
    PlayerConfiguration,
    PrivateModel,
    Registration,
    RequestGrant,
    ScriptedRegistration,
    Stop,
    Stopped,
)


class NativeContainer:
    """One player owns HTTP, response publication, and its ordinary tick executor."""

    def __init__(
        self,
        configuration: PlayerConfiguration,
        registration: Registration,
        endpoint: str,
        send: Callable[[str], Coroutine[Any, Any, None]],
    ):
        if not isinstance(registration, (NativeRegistration, ScriptedRegistration)):
            raise ValueError("bundled macro container does not execute external research policies")
        if registration.slot != configuration.slot:
            raise ValueError("container registration differs from configured seat")
        self.configuration = configuration
        self.registration = registration.model_copy(deep=True)
        if isinstance(registration, NativeRegistration):
            LearnerScope(endpoint=endpoint, slot=configuration.slot, model=registration.profile.model)
        self.send_wire = send
        self.send_lock = asyncio.Lock()
        baseline = registration.baseline if isinstance(registration, ScriptedRegistration) else "brigade"
        self.executor = MacroExecutor(configuration.policy_env, configuration.slot, configuration.layout, baseline)
        self.worker = PlanWorker(configuration.slot, endpoint, self.send)
        self.reply_owner: asyncio.Task | None = None
        self.stop_request: Stop | None = None
        self.finished = False
        self.deadline = float("inf")

    async def send(self, message: PrivateModel) -> None:
        """Serialize received progress, replies, and ACKs under one bounded owner."""
        deadline = min(self.deadline, lifecycle.cleanup_deadline())
        if deadline <= asyncio.get_running_loop().time():
            raise lifecycle.OwnershipUnsettled("player socket writer has no remaining owner budget")
        async with lifecycle.own_until(deadline), asyncio.timeout_at(deadline):
            async with self.send_lock:
                sending = lifecycle.owned_task(self.send_wire(message.model_dump_json()))
                if not await lifecycle.settle({sending}, deadline, cancel=False):
                    raise lifecycle.OwnershipUnsettled("player socket writer did not join")
                sending.result()

    async def relay_reply(self, task: asyncio.Task) -> None:
        await asyncio.shield(task)
        await self.send(task.result())

    async def receive(self, raw: str | bytes) -> None:
        message = GAME_FRAME.validate_json(raw)
        if isinstance(message, PlayerConfiguration):
            raise ValueError("player configuration cannot change after registration")
        if isinstance(message, Stop):
            if self.stop_request is not None and self.stop_request != message:
                raise ValueError("engine stop identity cannot change")
            if self.stop_request is not None:
                return
            self.stop_request = message
            deadline = min(self.deadline, lifecycle.cleanup_deadline())
            self.deadline = deadline
            worker_joined = await self.worker.stop(deadline)
            replies_joined = await self.settle_replies(deadline)
            if not worker_joined or not replies_joined:
                raise lifecycle.OwnershipUnsettled("player stop left native reader or reply writer unresolved")
            await self.send(Stopped(request_id=message.request_id, joined=True))
            return
        if isinstance(message, EvidenceReceived):
            if self.stop_request is None or message.request_id != self.stop_request.request_id:
                raise ValueError("engine evidence receipt has no matching stopped identity")
            self.finished = True
            return
        if self.stop_request is not None:
            raise ValueError("engine private window follows player stop")
        if isinstance(message, PhysicalObservation):
            self.executor.issue(message)
            action = PhysicalAction(
                request_id=message.request_id,
                slot=self.configuration.slot,
                action_name=self.executor.expected,
            )
            self.executor.admit(action)
            await self.send(action)
        elif isinstance(message, DirectiveInstalled):
            self.executor.install(message)
        else:
            assert isinstance(message, RequestGrant)
            if (
                not isinstance(self.registration, NativeRegistration)
                or message.window.slot != self.configuration.slot
                or message.window.alias != self.configuration.alias
                or message.window.profile != self.registration.profile
            ):
                raise ValueError("native grant differs from frozen container policy")
            if self.reply_owner is not None and not self.reply_owner.done():
                raise ValueError("native grant overlaps prior response publisher")
            if self.reply_owner is not None and not self.reply_owner.cancelled():
                self.reply_owner.result()
            self.reply_owner = lifecycle.owned_task(self.relay_reply(self.worker.start(message)))

    async def close_owner(self, deadline: float) -> bool:
        """No implicit socket context or executor cancellation sweep establishes a join."""
        self.deadline = min(self.deadline, deadline)
        worker_joined = await self.worker.stop(self.deadline)
        replies_joined = await self.settle_replies(self.deadline)
        return worker_joined and replies_joined

    async def settle_replies(self, deadline: float) -> bool:
        if self.reply_owner is None:
            return True
        if not await lifecycle.settle({self.reply_owner}, deadline, cancel=True):
            return False
        if self.reply_owner.cancelled():
            return True
        error = self.reply_owner.exception()
        if isinstance(error, lifecycle.OwnershipUnsettled):
            return False
        if error is not None:
            raise error
        return True

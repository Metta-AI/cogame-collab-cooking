"""One absolute cleanup budget for work owned by an episode process."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Coroutine
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TypeVar

CLEANUP_SECONDS = 2.0
T = TypeVar("T")


@dataclass
class Owner:
    tasks: set[asyncio.Task] = field(default_factory=set)
    deadline: float | None = None
    parent: Owner | None = None
    stopping: bool = False


current_owner: ContextVar[Owner | None] = ContextVar("current_owner", default=None)


class OwnershipUnsettled(RuntimeError):
    """An acquired reader or writer did not finish within its owner's budget."""


def owned_task(work: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
    task = asyncio.create_task(work)
    owner = current_owner.get()
    while owner is not None:
        owner.tasks.add(task)
        task.add_done_callback(owner.tasks.discard)
        owner = owner.parent
    return task


async def settle(tasks: set[asyncio.Task], deadline: float, *, cancel: bool) -> bool:
    """Cancellation is a request; only an observed terminal task establishes a join."""
    pending = {task for task in tasks if not task.done()}
    if cancel:
        for task in pending:
            if not task.cancelling():
                task.cancel()
    if pending:
        _, pending = await asyncio.wait(pending, timeout=max(0, deadline - asyncio.get_running_loop().time()))
    for task in tasks - pending:
        if not task.cancelled():
            task.exception()
    return not pending


async def settle_owner(owner: Owner, deadline: float, *, cancel: bool, excluding=frozenset()) -> bool:
    """Join every registered descendant, including children spawned during cleanup."""
    while pending := {task for task in owner.tasks - excluding if not task.done()}:
        if not await settle(pending, deadline, cancel=cancel):
            return False
    return True


def cleanup_deadline() -> float:
    deadline = asyncio.get_running_loop().time() + CLEANUP_SECONDS
    owner = current_owner.get()
    while owner is not None:
        if owner.deadline is not None:
            deadline = min(deadline, owner.deadline)
        owner = owner.parent
    return deadline


@asynccontextmanager
async def own_until(deadline: float):
    """Nest a phase budget without resetting its process termination deadline."""
    owner = Owner(deadline=deadline, parent=current_owner.get())
    token = current_owner.set(owner)
    try:
        yield owner
    finally:
        joined = await settle_owner(owner, cleanup_deadline(), cancel=True)
        current_owner.reset(token)
        if not joined:
            raise OwnershipUnsettled("phase cleanup left owned work unresolved")


async def run_owned(work: Coroutine[Any, Any, T]) -> T | None:
    owner = Owner()
    token = current_owner.set(owner)
    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()
    task = asyncio.create_task(work)

    stop_requested = False

    def receive_signal(signum: int, frame: Any) -> None:
        nonlocal stop_requested
        if not stop_requested:
            stop_requested = True
            owner.stopping = True
            owner.deadline = loop.time() + CLEANUP_SECONDS
            loop.call_soon_threadsafe(stopping.set)

    previous = {sig: signal.signal(sig, receive_signal) for sig in (signal.SIGTERM, signal.SIGINT)}
    signal_task = asyncio.create_task(stopping.wait())
    try:
        done, _ = await asyncio.wait({task, signal_task}, return_when=asyncio.FIRST_COMPLETED)
        if task not in done:
            assert owner.deadline is not None
            if not await settle({task}, owner.deadline, cancel=True):
                raise OwnershipUnsettled("signal shutdown left owned work unresolved")
        if task.cancelled() and stopping.is_set():
            return None
        return task.result()
    finally:
        if stopping.is_set():
            for sig in previous:
                signal.signal(sig, signal.SIG_IGN)
        else:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        deadline = cleanup_deadline()
        signal_joined = await settle({signal_task}, deadline, cancel=True)
        joined = await settle_owner(owner, deadline, cancel=True) and signal_joined
        current_owner.reset(token)
        if not joined:
            raise OwnershipUnsettled("process cleanup left owned work unresolved")


def main_owned(work: Coroutine[Any, Any, T]) -> T | None:
    """Avoid asyncio.run's unbounded cancellation sweep after an unresolved join."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(run_owned(work))
    finally:
        loop.close()

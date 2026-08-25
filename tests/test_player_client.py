"""The bundled player's dial: it retries, and the retry is bounded.

The game container's uvicorn is not necessarily listening when the player pods
dial -- the platform starts them together, and `docker_smoke.sh` starts them
back to back. A refused connection used to be caught by `main` and turned into
`exit 0`, so the seat noop-ed the whole episode and every gate stayed green:
in CI run 32812571422 two of four seats, including the prompt seat, never
arrived and the smoke passed.
"""

from __future__ import annotations

import asyncio

import pytest

from collab_cooking.coworld.player import connect_with_retry


def test_the_dial_retries_until_the_game_is_listening() -> None:
    attempts: list[int] = []
    slept: list[float] = []

    async def dial() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionRefusedError("[Errno 111] Connection refused")
        return "socket"

    async def sleep(delay: float) -> None:
        slept.append(delay)

    socket = asyncio.run(
        connect_with_retry("ws://game:8080/player", connect=dial, sleep=sleep, now=lambda: 0.0)
    )
    assert socket == "socket"
    assert len(attempts) == 3
    assert slept == [0.25, 0.5], "the backoff doubles"


def test_the_dial_is_bounded() -> None:
    """`degrade, never hang`: the retry has an explicit deadline, and the
    caller (`main`) still exits 0 when it runs out."""
    attempts: list[int] = []
    clock = iter([0.0, 10.0, 70.0])

    async def dial() -> str:
        attempts.append(1)
        raise ConnectionRefusedError("[Errno 111] Connection refused")

    async def sleep(_delay: float) -> None:
        return None

    with pytest.raises(ConnectionRefusedError):
        asyncio.run(
            connect_with_retry(
                "ws://game:8080/player",
                deadline_seconds=60,
                connect=dial,
                sleep=sleep,
                now=lambda: next(clock),
            )
        )
    assert len(attempts) == 2, "it stops dialling once the deadline has passed"

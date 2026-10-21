"""Owned WebSocket lifecycle for the native macro container."""

from __future__ import annotations

import asyncio
import socket as socket_module
from collections.abc import Callable
from urllib.parse import urlsplit

from pydantic import ValidationError
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from . import lifecycle
from .native_container import NativeContainer
from .plan_protocol import MAX_FRAME_BYTES, PlayerConfiguration, Registration
from .resolver import OwnedResolver

CONNECT_SECONDS = 60.0
EPISODE_SECONDS = 20 * 60.0


async def play(
    url: str,
    registration: Callable[[PlayerConfiguration], Registration],
    endpoint: str,
) -> None:
    """A joined stop receipt ends play; EOF alone cannot supply ownership credit."""
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("ws", "wss")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("player requires the exact assigned game WebSocket URL")
    loop = asyncio.get_running_loop()
    episode_deadline = loop.time() + EPISODE_SECONDS
    connect_deadline = loop.time() + CONNECT_SECONDS
    socket: ClientConnection | None = None
    container: NativeContainer | None = None
    reading: asyncio.Task | None = None
    connecting: asyncio.Task | None = None
    handling: asyncio.Task | None = None
    configured: asyncio.Task | None = None
    addresses = await OwnedResolver().resolve(
        parsed.hostname, parsed.port or (443 if parsed.scheme == "wss" else 80), family=socket_module.AF_UNSPEC
    )
    address = addresses[0]
    dial_index = 0

    async def dial() -> ClientConnection:
        # Keep the assigned URI/Host/TLS identity, replacing only the DNS dial target.
        return await connect(
            url,
            host=address["host"],
            port=address["port"],
            proxy=None,
            max_size=MAX_FRAME_BYTES,
            max_queue=4,
            open_timeout=min(5, max(0, connect_deadline - loop.time())),
            close_timeout=1,
        )

    try:
        while socket is None:
            if loop.time() >= connect_deadline:
                raise TimeoutError("game connection exceeded its startup budget")
            address = addresses[dial_index % len(addresses)]
            dial_index += 1
            connecting = lifecycle.owned_task(dial())
            if not await lifecycle.settle({connecting}, connect_deadline, cancel=False):
                raise lifecycle.OwnershipUnsettled("game socket acquisition did not settle")
            error = connecting.exception()
            if error is None:
                socket = connecting.result()
            elif isinstance(error, (OSError, WebSocketException)):
                await asyncio.sleep(min(0.25, max(0, connect_deadline - loop.time())))
            else:
                raise error

        async with asyncio.timeout_at(episode_deadline):
            reading = lifecycle.owned_task(socket.recv())
            await asyncio.wait({reading})
            error = reading.exception()
            if error is not None:
                raise RuntimeError("game closed before player configuration") from None

            async def configure() -> PlayerConfiguration:
                return PlayerConfiguration.model_validate_json(reading.result())

            configured = lifecycle.owned_task(configure())
            await asyncio.wait({configured})
            if configured.exception() is not None:
                raise ValueError("game supplied an invalid private player configuration") from None
            configuration = configured.result()
            frozen = registration(configuration)
            container = NativeContainer(configuration, frozen, endpoint, socket.send)
            await container.send(frozen)
            while not container.finished:
                reading = lifecycle.owned_task(socket.recv())
                if not await lifecycle.settle({reading}, min(episode_deadline, container.deadline), cancel=False):
                    raise TimeoutError("game evidence receipt exceeded the absolute owner budget")
                error = reading.exception()
                if isinstance(error, ConnectionClosed):
                    raise RuntimeError("game closed before its joined evidence receipt") from None
                if error is not None:
                    raise error
                handling = lifecycle.owned_task(container.receive(reading.result()))
                await asyncio.wait({handling})
                error = handling.exception()
                if isinstance(error, ValidationError):
                    raise ValueError("game supplied an invalid private player frame") from None
                if error is not None:
                    raise error
    finally:
        deadline = min(lifecycle.cleanup_deadline(), float("inf") if container is None else container.deadline)
        pending = {task for task in (reading, connecting, handling, configured) if task is not None}
        readers_joined = await lifecycle.settle(pending, deadline, cancel=True)
        if socket is None and connecting is not None and connecting.done() and not connecting.cancelled():
            if connecting.exception() is None:
                socket = connecting.result()
        native_joined = container is None or await container.close_owner(deadline)
        socket_joined = True
        if socket is not None:
            closing = lifecycle.owned_task(socket.close())
            socket_joined = await lifecycle.settle({closing}, deadline, cancel=False)
        if not readers_joined or not native_joined or not socket_joined:
            raise lifecycle.OwnershipUnsettled("player socket or native owner did not join")

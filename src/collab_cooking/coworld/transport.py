"""Pinned HTTP transport retains URL/Host while DNS and dials have owned joins."""

import asyncio
import ipaddress
import socket
import ssl

import httpcore
import httpx
from httpcore._backends.anyio import AnyIOBackend

from . import lifecycle
from .resolver import OwnedResolver


class OwnedNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self):
        self.backend = AnyIOBackend()
        self.resolver = OwnedResolver()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        numeric = ":" in host or (host.count(".") == 3 and all(part.isdigit() for part in host.split(".")))
        addresses = (
            [{"host": str(ipaddress.ip_address(host))}]
            if numeric
            else await self.resolver.resolve(host, port, socket.AF_UNSPEC)
        )
        failures = []
        for address in addresses:
            connection = lifecycle.owned_task(
                self.backend.connect_tcp(
                    address["host"],
                    port,
                    timeout,
                    local_address,
                    socket_options,
                )
            )
            selected = False
            try:
                done, _pending = await asyncio.wait({connection}, timeout=timeout)
                if not done:
                    raise httpcore.ConnectTimeout("owned sidecar dial exceeded connect timeout")
                if connection.cancelled():
                    raise asyncio.CancelledError
                failure = connection.exception()
                if failure is not None:
                    failures.append(failure)
                    continue
                selected = True
                return connection.result()
            finally:
                if not selected:
                    deadline = lifecycle.cleanup_deadline()
                    if not await lifecycle.settle({connection}, deadline, cancel=True):
                        raise lifecycle.OwnershipUnsettled("sidecar dial owner did not join")
                    if not connection.cancelled() and connection.exception() is None:
                        closing = lifecycle.owned_task(connection.result().aclose())
                        if not await lifecycle.settle({closing}, deadline, cancel=False):
                            raise lifecycle.OwnershipUnsettled("unselected sidecar connection did not join")
                        closing.result()
        raise failures[-1]

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise ValueError("native sidecar profile does not use Unix sockets")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class OwnedHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self):
        # HTTPX0.28.1 owns this pool and its native exception/response adapters.
        # The HTTPcore1.0.9 backend hook changes DNS ownership, not URL/Host/SNI.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            network_backend=OwnedNetworkBackend(),
        )

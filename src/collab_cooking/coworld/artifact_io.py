"""Platform artifact HTTP uses one absolute budget and observed reader joins."""

import asyncio
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from . import lifecycle
from .transport import OwnedHTTPTransport

ARTIFACT_SECONDS = 30.0
MAX_CONFIG_BYTES = 16 * 1024 * 1024
MAX_ACK_BYTES = 1024 * 1024


async def exchange(uri: str, *, data: bytes | None, method: str, limit: int, deadline: float) -> bytes:
    client = httpx.AsyncClient(transport=OwnedHTTPTransport(), timeout=None)
    response: httpx.Response | None = None

    async def receive() -> bytes:
        nonlocal response
        response = await client.send(
            client.build_request(method, uri, content=data, headers={"Content-Type": "application/json"}),
            stream=True,
        )
        if not 200 <= response.status_code < 300:
            raise ValueError(f"platform artifact HTTP status {response.status_code}")
        received = bytearray()
        async for chunk in response.aiter_bytes():
            received.extend(chunk)
            if len(received) > limit:
                raise ValueError("platform artifact response exceeds its bounded capacity")
        return bytes(received)

    reader = lifecycle.owned_task(receive())
    try:
        done, _ = await asyncio.wait({reader}, timeout=max(0, deadline - asyncio.get_running_loop().time()))
        if reader not in done:
            raise TimeoutError("platform artifact reached its absolute deadline")
        return reader.result()
    finally:
        drain = lifecycle.cleanup_deadline()
        joined = await lifecycle.settle({reader}, drain, cancel=True)

        async def close() -> None:
            try:
                if response is not None:
                    await response.aclose()
            finally:
                await client.aclose()

        if joined:
            closer = lifecycle.owned_task(close())
            joined = await lifecycle.settle({closer}, drain, cancel=False)
            if joined:
                closer.result()
        if not joined:
            raise lifecycle.OwnershipUnsettled("platform artifact reader or transport did not join")


async def read_uri(uri: str, *, deadline: float) -> bytes:
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        path = Path(unquote(parsed.path))
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise ValueError("platform config exceeds its bounded capacity")
        return path.read_bytes()
    if parsed.scheme in {"http", "https"}:
        return await exchange(uri, data=None, method="GET", limit=MAX_CONFIG_BYTES, deadline=deadline)
    raise ValueError("unsupported platform artifact URI scheme")


async def write_uri(uri: str, data: bytes, *, method: str | None, deadline: float) -> None:
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        path = Path(unquote(parsed.path))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return
    if parsed.scheme in {"http", "https"}:
        await exchange(uri, data=data, method=method or "PUT", limit=MAX_ACK_BYTES, deadline=deadline)
        return
    raise ValueError("unsupported platform artifact URI scheme")

"""Actual artifact sockets terminate under the owned absolute publication budget."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from collab_cooking.coworld import lifecycle
from collab_cooking.coworld.artifact_io import read_uri, write_uri


@asynccontextmanager
async def artifact_server(*, trickle: bool):
    owners = set()
    requests = []

    async def handle(reader, writer):
        task = asyncio.current_task()
        owners.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            requests.append(head)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n"
                if trickle
                else b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"
            )
            await writer.drain()
            if trickle:
                for _ in range(100):
                    writer.write(b"x")
                    await writer.drain()
                    await asyncio.sleep(0.1)
        finally:
            writer.close()
            await writer.wait_closed()
            owners.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/artifact", requests
    finally:
        server.close()
        await server.wait_closed()
        await lifecycle.settle(owners.copy(), asyncio.get_running_loop().time() + 2, cancel=True)


@pytest.mark.asyncio
async def test_actual_file_and_http_artifact_contract(tmp_path):
    path = tmp_path / "config.json"
    await write_uri(path.as_uri(), b"{}", method=None, deadline=asyncio.get_running_loop().time() + 1)
    assert await read_uri(path.as_uri(), deadline=asyncio.get_running_loop().time() + 1) == b"{}"
    async with artifact_server(trickle=False) as (uri, requests):
        assert await read_uri(uri, deadline=asyncio.get_running_loop().time() + 1) == b"{}"
        await write_uri(uri, b"{}", method="POST", deadline=asyncio.get_running_loop().time() + 1)
        assert requests[0].startswith(b"GET /artifact ")
        assert requests[1].startswith(b"POST /artifact ")


@pytest.mark.asyncio
async def test_actual_slow_trickle_has_absolute_deadline_and_owned_join():
    async with artifact_server(trickle=True) as (uri, _):
        async with lifecycle.own_until(asyncio.get_running_loop().time() + 3) as owner:
            start = asyncio.get_running_loop().time()
            with pytest.raises(TimeoutError):
                await read_uri(uri, deadline=start + 0.15)
            assert asyncio.get_running_loop().time() - start < 1
            assert not any(not task.done() for task in owner.tasks)

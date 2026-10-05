"""Real game sockets require the engine's joined receipt and private error hygiene."""

import asyncio
import json
from uuid import uuid4

import pytest
from websockets.asyncio.server import serve

from collab_cooking.coworld.plan_protocol import (
    MAX_FRAME_BYTES,
    EvidenceReceived,
    PlayerConfiguration,
    ScriptedRegistration,
    Stop,
    Stopped,
)
from collab_cooking.coworld.player_socket import play
from tests.harness import build_episode


@pytest.mark.asyncio
@pytest.mark.parametrize("with_receipt", [True, False])
async def test_actual_joined_stop_requires_engine_receipt_before_eof(with_receipt):
    episode = build_episode({"layout": "open-kitchen", "max_steps": 900})
    configuration = PlayerConfiguration(slot=0, alias="Cog-A", layout="open-kitchen", policy_env=episode.policy_env)
    stopped = []

    async def game(socket):
        assert socket.request.path == "/assigned-game?token=fixture"
        assert socket.request.headers["Host"].startswith("localhost:")
        await socket.send(configuration.model_dump_json())
        registration = ScriptedRegistration.model_validate_json(await socket.recv())
        assert registration.slot == 0 and registration.baseline == "brigade"
        stop = Stop(request_id=uuid4())
        await socket.send(stop.model_dump_json())
        acknowledgement = Stopped.model_validate_json(await socket.recv())
        assert acknowledgement.request_id == stop.request_id and acknowledgement.joined is True
        stopped.append(acknowledgement)
        if with_receipt:
            await socket.send(EvidenceReceived(request_id=stop.request_id).model_dump_json())

    server = await serve(game, "127.0.0.1", 0, max_size=MAX_FRAME_BYTES)
    url = f"ws://localhost:{server.sockets[0].getsockname()[1]}/assigned-game?token=fixture"
    try:
        request = play(
            url,
            lambda configured: ScriptedRegistration(slot=configured.slot, baseline="brigade"),
            "http://127.0.0.1:8080",
        )
        if with_receipt:
            await asyncio.wait_for(request, 3)
        else:
            with pytest.raises(RuntimeError, match="before its joined evidence receipt"):
                await asyncio.wait_for(request, 3)
        assert len(stopped) == 1
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_actual_malformed_private_frame_does_not_expose_input_in_traceback():
    episode = build_episode({"layout": "open-kitchen", "max_steps": 900})
    configuration = PlayerConfiguration(slot=0, alias="Cog-A", layout="open-kitchen", policy_env=episode.policy_env)
    sentinel = "PRIVATE_GAME_INPUT_SENTINEL"

    async def game(socket):
        await socket.send(configuration.model_dump_json())
        await socket.recv()
        await socket.send(json.dumps({"type": sentinel, "private": sentinel}))
        await socket.wait_closed()

    server = await serve(game, "127.0.0.1", 0, max_size=MAX_FRAME_BYTES)
    try:
        task = asyncio.create_task(
            play(
                f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                lambda configured: ScriptedRegistration(slot=configured.slot, baseline="brigade"),
                "http://127.0.0.1:8080",
            )
        )
        await asyncio.wait({task}, timeout=3)
        assert task.done()
        error = task.exception()
        assert isinstance(error, ValueError)
        import traceback

        rendered = "".join(traceback.format_exception(error))
        assert sentinel not in rendered
        assert "invalid private player frame" in rendered
    finally:
        server.close()
        await server.wait_closed()

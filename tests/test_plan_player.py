"""Actual native HTTP and private socket progress reach independent game parsing."""

import asyncio
import base64
import json
from uuid import uuid4

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from collab_cooking.coworld import lifecycle, plan_player
from collab_cooking.coworld.native_container import NativeContainer
from collab_cooking.coworld.plan_admission import PlanAdmission
from collab_cooking.coworld.plan_player import PlanWorker
from collab_cooking.coworld.plan_protocol import (
    MAX_FRAME_BYTES,
    AttemptProgress,
    NativeProfile,
    NativeRegistration,
    PlanReply,
    PlanWindow,
    PlayerConfiguration,
    RequestGrant,
)
from tests.harness import build_episode


@pytest.mark.asyncio
@pytest.mark.parametrize("context_tokens", [None, 32768])
async def test_native_received_bytes_cross_actual_socket_before_independent_plan_acceptance(context_tokens):
    text = '{"station":"pass","say":"shared","note":"PRIVATE_WORKER_SENTINEL"}'
    payload = {
        "model": "checkpoint:fixture",
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 11, "output_tokens": 19},
        "stop_reason": "end_turn",
    }
    if context_tokens is not None:
        # Synthetic capacity evidence proves frame delivery, not serving authenticity.
        payload["usage"]["input_tokens"] = context_tokens
        payload["sampling_evidence"] = {
            "policy_revision": "fixture-policy",
            "tokenizer_revision": "fixture-tokenizer",
            "chat_template": "fixture-template",
            "sampling": "full_softmax_temperature_one",
            "enable_thinking": False,
            "max_new_tokens": 900,
            "max_sequence_length": 33700,
            "sampling_seed": 7,
            "eos_token_ids": [2],
            "prompt_token_ids": list(range(context_tokens)),
            "completion_token_ids": [*range(3, 21), 2],
            "behavior_log_probs": [-1.0] * 19,
            "response": text,
            "stop_reason": "eos",
        }
    wire = json.dumps(payload, indent=2).encode()
    handlers = set()
    calls = []

    async def http(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = int(
                next(
                    line.split(b":", 1)[1]
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
            )
            calls.append((headers, json.loads(await reader.readexactly(length))))
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                + f"Content-Length: {len(wire)}\r\n\r\n".encode()
                + wire
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    http_server = await asyncio.start_server(http, "127.0.0.1", 0)
    admission = PlanAdmission(26)
    window = PlanWindow(
        window_id=uuid4(),
        slot=2,
        turn=1,
        step=50,
        alias="Cog-C",
        system="Ordinary private kitchen instructions",
        user="Exact observed board: soup 1",
        legal_stations=["board", "pass"],
        ally_aliases=["none"],
        observation={"tick": 50, "board": {"soup": 1}},
        profile=NativeProfile(model="checkpoint:fixture"),
    )
    grant = admission.begin([window], 12)[0]
    accepted = asyncio.get_running_loop().create_future()

    async def game(socket):
        await socket.send(grant.model_dump_json())
        while True:
            raw = await socket.recv()
            value = json.loads(raw)
            if value["type"] == "plan_progress":
                admission.progress(2, AttemptProgress.model_validate_json(raw))
            else:
                reply = PlanReply.model_validate_json(raw)
                accepted.set_result(await admission.reply(2, reply))
                return

    socket_server = await serve(game, "127.0.0.1", 0, max_size=MAX_FRAME_BYTES)
    endpoint = f"http://127.0.0.1:{http_server.sockets[0].getsockname()[1]}"
    try:
        async with connect(
            f"ws://127.0.0.1:{socket_server.sockets[0].getsockname()[1]}", max_size=MAX_FRAME_BYTES
        ) as socket:
            received = RequestGrant.model_validate_json(await socket.recv())

            episode = build_episode({"layout": "open-kitchen", "max_steps": 900})
            configuration = PlayerConfiguration(
                slot=2, alias="Cog-C", layout="open-kitchen", policy_env=episode.policy_env
            )
            registration = NativeRegistration(slot=2, prompt="ordinary strategy", profile=window.profile)
            container = NativeContainer(configuration, registration, endpoint, socket.send)
            await container.receive(received.model_dump_json())
            await container.reply_owner
            parsed = await asyncio.wait_for(accepted, 2)
            assert parsed.station == "pass" and parsed.note == "PRIVATE_WORKER_SENTINEL"
            assert "PRIVATE_WORKER_SENTINEL" not in json.dumps(parsed.replay_event(2, "Cog-C", 1, "llm"))
            evidence = admission.windows[2].attempts[grant.attempt_id]
            assert evidence.raw_response.encode() == wire
            assert evidence.response_reader_joined is True and evidence.response_complete is True
            assert evidence.model == "checkpoint:fixture" and evidence.platform_call_id is None
            if context_tokens is None:
                assert evidence.sampling_evidence is None
            else:
                assert len(wire) > 65536
                assert evidence.sampling_evidence.prompt_token_ids == list(range(context_tokens))
            assert calls[0][1] == window.native_request(grant.user).model_dump(exclude_none=True)
            assert b"x-coworld-player-slot: 2" in calls[0][0].lower()
            assert await container.close_owner(lifecycle.cleanup_deadline())
    finally:
        socket_server.close()
        await socket_server.wait_closed()
        http_server.close()
        await http_server.wait_closed()
        await asyncio.gather(*handlers)


@pytest.mark.asyncio
async def test_stop_preserves_actual_partial_invalid_utf8_and_joins_before_credit():
    observed = asyncio.Event()
    handlers = set()
    snapshots = []
    prefix = b'{"content": "\xe2\x82'

    async def http(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n" + prefix)
            await writer.drain()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    async def progress(value):
        snapshots.append(value.evidence)
        if value.evidence.response_body_b64:
            observed.set()

    server = await asyncio.start_server(http, "127.0.0.1", 0)
    window = PlanWindow(
        window_id=uuid4(),
        slot=0,
        turn=1,
        step=50,
        alias="Cog-A",
        system="instructions",
        user="private-view",
        legal_stations=["pass"],
        ally_aliases=["none"],
        observation={"tick": 50},
        profile=NativeProfile(),
    )
    worker = PlanWorker(0, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", progress)
    worker.start(PlanAdmission(26).begin([window], 12)[0])
    try:
        await asyncio.wait_for(observed.wait(), 2)
        deadline = asyncio.get_running_loop().time() + 0.5
        assert await worker.stop(deadline)
        assert await worker.stop(deadline)
        assert worker.owner.deadline == deadline
        final = snapshots[-1]
        assert base64.b64decode(final.response_body_b64) == prefix
        assert final.raw_response is None
        assert final.http_status == 200 and final.response_complete is False
        assert final.response_reader_joined is True
        assert worker.writer.task.done() and not worker.owner.tasks
        count = len(snapshots)
        await asyncio.sleep(0)
        assert len(snapshots) == count
    finally:
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers)


@pytest.mark.asyncio
async def test_uncooperative_native_owner_denies_stop_credit_under_one_deadline(monkeypatch):
    class Unsettled(asyncio.Future):
        def cancel(self, msg=None):
            return False

    acquired = asyncio.Event()
    release = Unsettled()

    async def unresolved(*args, **kwargs):
        acquired.set()
        await release

    async def progress(value):
        raise AssertionError("an unobserved response cannot emit received facts")

    monkeypatch.setattr(plan_player, "complete", unresolved)
    window = PlanWindow(
        window_id=uuid4(),
        slot=0,
        turn=1,
        step=50,
        alias="Cog-A",
        system="instructions",
        user="private-view",
        legal_stations=["pass"],
        ally_aliases=["none"],
        observation={"tick": 50},
        profile=NativeProfile(),
    )
    worker = PlanWorker(0, "http://127.0.0.1:1234", progress)
    task = worker.start(PlanAdmission(26).begin([window], 12)[0])
    await acquired.wait()
    deadline = asyncio.get_running_loop().time() + 0.02
    assert not await worker.stop(deadline)
    assert not await worker.stop(deadline)
    assert worker.owner.deadline == deadline
    assert worker.attempt.response_reader_joined is None
    assert worker.attempt.response_body_b64 is None
    release.set_result(None)
    await asyncio.wait({task, *worker.owner.tasks}, timeout=0.5)
    assert task.done()
    assert isinstance(task.exception(), lifecycle.OwnershipUnsettled)
    # Test-owned resources can settle later; that never retroactively grants stop credit.
    assert await lifecycle.settle_owner(worker.owner, asyncio.get_running_loop().time() + 0.5, cancel=True)

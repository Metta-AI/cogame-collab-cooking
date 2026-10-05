"""The actual game loop admits ordinary container controls before stepping the engine."""

import asyncio
import base64
import json

import pytest
from coworld.decision_trajectory import CompleteEpisode

from collab_cooking.coworld import lifecycle, replay
from collab_cooking.coworld.plan_protocol import NativeProfile
from tests.harness import FakeSocket, build_episode, fast_cert_config, run_episode, scripted_registration


def test_all_four_containers_complete_actual_engine_ticks_and_joined_stop(tmp_path):
    output = run_episode(
        fast_cert_config(max_steps=40, step_seconds=0.0, shutdown_grace_seconds=0.0),
        [scripted_registration(name) for name in ("brigade", "runner", "passer", "courier")],
        tmp_path,
        run_timeout=10,
    )
    episode = output["episode"]
    assert episode.sim.current_step == 40
    assert episode.done and episode.admission.sealed
    assert all(channel.acknowledged for channel in episode.channels)
    assert all(socket.container.finished for socket in output["sockets"])
    assert [len(socket.actions) for socket in output["sockets"]] == [40] * 4
    assert all(admitted.cause == "" for admitted in episode.installed_controls)
    assert episode.applied_control_buffers["actions"] == episode.sim._c_sim.actions().tolist()
    assert episode.applied_control_buffers["vibe_actions"] == episode.sim._c_sim.vibe_actions().tolist()
    assert episode.applied_actions == [control.installed_action for control in episode.installed_controls]
    assert output["results"]["scores"] == episode.scores()
    replay = json.loads(output["replay_bytes"])
    assert len(replay["ticks"]) == 40
    assert episode.admission.history == {}


@pytest.mark.asyncio
async def test_four_parallel_native_http_plans_reach_actual_engine_installation_and_stop(tmp_path, monkeypatch):
    """Synthetic HTTP proves transport/parser/executor joins, never serving authority."""
    calls = []
    canonical_path = tmp_path / "trajectory.jsonl"
    monkeypatch.setenv("COGAME_SAVE_TRAJECTORY_URI", canonical_path.as_uri())
    monkeypatch.setenv("COWORLD_EPISODE_ID", "four-player-http-diagnostic")
    handlers = set()
    parallel = asyncio.Event()
    text = '{"station":"hold","say":"shared","note":"PRIVATE_NATIVE_EPISODE_SENTINEL"}'
    wire = json.dumps(
        {
            "model": "checkpoint:local-http-diagnostic",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 11, "output_tokens": 19},
            "stop_reason": "end_turn",
        },
        indent=2,
    ).encode()

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
            if len(calls) == 4:
                parallel.set()
            await parallel.wait()
            writer.write(
                b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(wire)}\r\n\r\n".encode()
                + wire
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(http, "127.0.0.1", 0)
    endpoint = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    episode = build_episode(
        fast_cert_config(
            max_steps=40,
            step_seconds=0.01,
            plan_interval_steps=10,
            min_plan_interval_seconds=0.0,
            shutdown_grace_seconds=0.0,
        )
    )
    artifacts = []
    episode.configure_artifacts(
        lambda result, raw: artifacts.append((result, raw)), generated_at="2026-10-04T00:00:00Z"
    )
    sockets = []
    try:
        for slot in range(4):
            socket = FakeSocket(
                {
                    "type": "register",
                    "kind": "native",
                    "prompt": "  Raw policy strategy.  ",
                    "profile": NativeProfile(model="checkpoint:local-http-diagnostic").model_dump(),
                },
                endpoint,
            )
            socket.episode = episode
            socket.connection_id = await episode.connect_player(slot, socket)
            await socket.register()
            sockets.append(socket)
        await asyncio.wait_for(episode.run(), 10)
        assert parallel.is_set()
        assert episode.sim.current_step == 40 and episode.done and episode.admission.sealed
        assert all(channel.acknowledged for channel in episode.channels)
        assert all(socket.container.finished for socket in sockets)
        accepted = [entry for entry in episode.admission.history.values() if entry.parsed_plan is not None]
        assert {entry.window.slot for entry in accepted} == {0, 1, 2, 3}
        assert all(entry.window.window_id in episode.delivered_windows for entry in accepted)
        for entry in accepted:
            assert entry.parsed_plan.note == "PRIVATE_NATIVE_EPISODE_SENTINEL"
            assert entry.window.system.endswith("STANDING ORDERS:   Raw policy strategy.  ")
            final = entry.attempts[entry.grants[-1].attempt_id]
            assert final.raw_response.encode() == base64.b64decode(final.response_body_b64) == wire
            assert final.response_reader_joined is True and final.response_complete is True
            assert final.model == "checkpoint:local-http-diagnostic"
            assert final.platform_call_id is None and final.sampling_evidence is None
        for headers, request in calls:
            assert request["model"] == "checkpoint:local-http-diagnostic" and request["temperature"] == 1.0
            assert b"x-coworld-player-slot:" in headers.lower()
        assert len(artifacts) == 1
        complete = CompleteEpisode.model_validate_json(canonical_path.read_bytes())
        assert complete.episode.status == "completed"
        assert len(complete.episode.outcome["registered_policies"]) == 4
        applied = [decision for decision in complete.decisions if decision.action_status == "accepted"]
        assert {decision.seat for decision in applied} == {"0", "1", "2", "3"}
        assert all(decision.execution.control_encoding == "u8-u8" for decision in applied)
        assert all(decision.attempts[-1].platform_call_id is None for decision in applied)
        assert "PRIVATE_NATIVE_EPISODE_SENTINEL" not in artifacts[0][1].decode()
        assert replay.capture(episode.sim, 4).step == 40
    finally:
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.gather(*handlers)


@pytest.mark.asyncio
async def test_unjoined_actual_game_receiver_withholds_public_artifacts_after_player_acks():
    episode = build_episode(fast_cert_config(max_steps=2, step_seconds=0.0, shutdown_grace_seconds=0.0))
    published = []
    episode.configure_artifacts(
        lambda result, raw: published.append((result, raw)), generated_at="2026-10-04T00:00:00Z"
    )
    sockets = []
    for slot in range(4):
        socket = FakeSocket(scripted_registration(), "")
        socket.episode = episode
        socket.connection_id = await episode.connect_player(slot, socket)
        await socket.register()
        sockets.append(socket)
    release = asyncio.Event()
    receiver = asyncio.create_task(release.wait())
    episode.player_receivers.add(receiver)
    try:
        with pytest.raises(lifecycle.OwnershipUnsettled):
            await episode.run()
        assert all(channel.stop_request is not None for channel in episode.channels)
        assert all(channel.acknowledged for channel in episode.channels)
        assert all(socket.container.finished for socket in sockets)
        assert episode.closing and not episode.done and not episode.admission.sealed
        assert published == []
        assert not receiver.done()
        with pytest.raises(ValueError, match="admission is closed"):
            episode._apply_actions(2)
    finally:
        release.set()
        await receiver

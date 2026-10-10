"""The game process joins the engine while its player sockets remain available."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from collab_cooking.coworld import lifecycle
from collab_cooking.coworld.server import serve_owned


class Episode:
    def __init__(self):
        self.play_task = None
        self.exited = False
        self.closed = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self):
        self.started.set()
        await self.release.wait()

    async def close_owner(self, deadline):
        assert not self.server.should_exit
        assert await lifecycle.settle({self.play_task}, deadline, cancel=True)
        self.closed = True
        return True


class Server:
    def __init__(self, episode):
        self.config = SimpleNamespace(
            loaded=True,
            app=SimpleNamespace(state=SimpleNamespace(game=SimpleNamespace(episode=episode))),
            lifespan_class=lambda _: self,
        )
        self.started = False
        self.should_exit = False
        self.shut = False
        self.episode = episode
        episode.server = self

    async def startup(self):
        self.started = True

    async def main_loop(self):
        await asyncio.Event().wait()

    async def shutdown(self):
        assert self.episode.closed
        assert self.should_exit
        self.shut = True


@pytest.mark.asyncio
async def test_cancel_joins_game_before_http_shutdown():
    episode = Episode()
    server = Server(episode)
    serving = asyncio.create_task(serve_owned(server, episode))
    await episode.started.wait()
    serving.cancel()
    assert await lifecycle.settle({serving}, asyncio.get_running_loop().time() + 1, cancel=False)
    assert serving.cancelled()
    assert episode.closed and episode.exited and server.shut


@pytest.mark.asyncio
async def test_game_failure_still_joins_http_owner():
    episode = Episode()

    async def failing_run():
        raise ValueError("engine failure")

    episode.run = failing_run
    server = Server(episode)
    with pytest.raises(ValueError, match="engine failure"):
        await serve_owned(server, episode)
    assert episode.closed and server.shut


@pytest.mark.asyncio
async def test_unresolved_engine_withholds_completion_but_closes_http(monkeypatch):
    episode = Episode()
    server = Server(episode)
    releasing = asyncio.Event()
    captured = []

    async def stalled_close(deadline):
        captured.append(asyncio.current_task())
        await releasing.wait()
        return False

    async def close_http():
        assert not episode.closed
        server.shut = True

    episode.close_owner = stalled_close
    server.shutdown = close_http
    monkeypatch.setattr(lifecycle, "cleanup_deadline", lambda: asyncio.get_running_loop().time() + 0.03)
    serving = asyncio.create_task(serve_owned(server, episode))
    await episode.started.wait()
    serving.cancel()
    assert await lifecycle.settle({serving}, asyncio.get_running_loop().time() + 0.1, cancel=False)
    assert isinstance(serving.exception(), lifecycle.OwnershipUnsettled)
    assert not episode.closed and server.shut
    assert captured and not captured[0].done()
    releasing.set()
    await captured[0]
    episode.release.set()
    await episode.play_task


@pytest.mark.asyncio
async def test_real_uvicorn_four_player_sockets_join_before_artifact_publication(tmp_path, monkeypatch):
    import json
    import time

    import uvicorn

    from collab_cooking.coworld import server as game_server
    from collab_cooking.coworld.plan_protocol import ScriptedRegistration
    from collab_cooking.coworld.player_socket import play
    from collab_cooking.coworld.server import create_app

    monkeypatch.setattr(game_server, "PROCESS_START", time.monotonic())
    from tests.harness import fast_cert_config

    results = tmp_path / "results.json"
    replay = tmp_path / "replay.json"
    monkeypatch.setenv("COGAME_RESULTS_URI", results.as_uri())
    monkeypatch.setenv("COGAME_SAVE_REPLAY_URI", replay.as_uri())
    private = tmp_path / "diagnostic-private.jsonl"
    monkeypatch.setenv("COGAME_SAVE_TRAJECTORY_URI", private.as_uri())
    monkeypatch.setenv("COWORLD_EPISODE_ID", "diagnostic-real-sockets")
    monkeypatch.setenv("COWORLD_SOURCE_REVISION", "a" * 40)
    monkeypatch.setenv("COWORLD_GAME_VERSION", "diagnostic")

    server = None

    def shutdown():
        server.should_exit = True

    app = create_app(fast_cert_config(max_steps=40, step_seconds=0.001, shutdown_grace_seconds=0), shutdown)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", ws_max_size=16 * 1024 * 1024, ws_max_queue=4)
    )
    episode = app.state.game.episode
    original_sink = episode._artifact_sink

    def publish(results_bytes, replay_bytes):
        assert episode.physics_task.done() and not episode.physics_task.cancelled()
        assert episode.admission.sealed
        original_sink(results_bytes, replay_bytes)

    episode._artifact_sink = publish
    serving = asyncio.create_task(serve_owned(server, app.state.game.episode))
    players = set()
    try:
        async with asyncio.timeout(12):
            while not server.started:
                assert not serving.done()
                await asyncio.sleep(0.01)
            port = server.servers[0].sockets[0].getsockname()[1]
            for slot in range(4):

                def register(configuration):
                    return ScriptedRegistration(slot=configuration.slot, baseline="brigade")

                players.add(
                    asyncio.create_task(
                        play(f"ws://127.0.0.1:{port}/player?slot={slot}&token=token-{slot}", register, "")
                    )
                )
            await asyncio.wait(players)
            for player in players:
                player.result()
            await serving
        episode = app.state.game.episode
        assert episode.done and episode.admission.sealed
        assert all(receiver.done() for receiver in episode.player_receivers)
        assert json.loads(results.read_text())["scores"] == episode.scores()
        assert len(json.loads(replay.read_text())["ticks"]) == 40
        records = [
            json.loads(line) for line in Path(str(private) + ".evidence.partial.jsonl").read_bytes().splitlines()
        ]
        applied = [record for record in records if record["kind"] == "engine_application"]
        assert len(applied) == 40
        assert [(record["start_tick"], record["end_tick"]) for record in applied] == [
            (tick, tick + 1) for tick in range(40)
        ]
        assert all(
            len(record["controls"]) == len(record["actions"]) == len(record["vibe_actions"]) == 4 for record in applied
        )
        assert applied[-1]["actions"] == episode.sim._c_sim.actions().tolist()
        assert applied[-1]["vibe_actions"] == episode.sim._c_sim.vibe_actions().tolist()
        assert sum(record["kind"] == "private_tick" for record in records) == 160
        assert records[-1]["kind"] == "ownership" and records[-1]["joined"]
        assert episode.journal.closed
        from coworld.decision_trajectory import CompleteEpisode

        canonical = CompleteEpisode.model_validate_json(private.read_bytes())
        assert canonical.episode.status == "completed"
        assert canonical.decisions == []  # Scripted physical controls are not native macro labels.
        assert (
            canonical.episode.outcome["physical_evidence_file"] == Path(str(private) + ".evidence.partial.jsonl").name
        )

    finally:
        assert await lifecycle.settle({serving} | players, lifecycle.cleanup_deadline(), cancel=True)


@pytest.mark.asyncio
async def test_unjoined_physics_keeps_private_spool_writable_and_withholds_publication(tmp_path, monkeypatch):
    import time

    from collab_cooking.coworld import server as game_server
    from collab_cooking.coworld.private_journal import OwnershipState
    from collab_cooking.coworld.server import create_app

    monkeypatch.setattr(game_server, "PROCESS_START", time.monotonic())
    from tests.harness import fast_cert_config

    monkeypatch.setenv("COGAME_SAVE_TRAJECTORY_URI", (tmp_path / "private.jsonl").as_uri())
    monkeypatch.setenv("COWORLD_EPISODE_ID", "diagnostic-unjoined-physics")
    monkeypatch.setenv("COGAME_RESULTS_URI", (tmp_path / "results.json").as_uri())
    monkeypatch.setenv("COGAME_SAVE_REPLAY_URI", (tmp_path / "replay.json").as_uri())
    app = create_app(fast_cert_config(max_steps=40), lambda: None)
    episode = app.state.game.episode
    entered = asyncio.Event()
    release = asyncio.Event()
    blocked = asyncio.Event()
    published = []

    async def physics():
        entered.set()
        try:
            await blocked.wait()
        finally:
            await release.wait()
            episode.journal.append(OwnershipState(joined=False, scope="engine", reason="late actual writer progress"))

    episode._play = physics
    episode._artifact_sink = lambda *args: published.append(args)
    monkeypatch.setattr(lifecycle, "cleanup_deadline", lambda: asyncio.get_running_loop().time() + 0.03)
    running = asyncio.create_task(episode.run())
    episode.play_task = running
    await entered.wait()
    running.cancel()
    assert await lifecycle.settle({running}, asyncio.get_running_loop().time() + 0.1, cancel=False)
    assert isinstance(running.exception(), lifecycle.OwnershipUnsettled)
    assert not episode.physics_task.done()
    assert not await episode.close_owner(asyncio.get_running_loop().time() + 0.03)
    assert not episode.journal.closed and not episode.done and not published
    release.set()
    assert await lifecycle.settle({episode.physics_task}, asyncio.get_running_loop().time() + 0.1, cancel=False)
    assert episode.physics_task.cancelled()
    assert b"late actual writer progress" in episode.journal.path.read_bytes()
    episode.journal.close_joined()

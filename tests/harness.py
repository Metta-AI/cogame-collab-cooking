"""Shared fixtures: an in-process episode with four scripted seats.

The game and the player are the SAME image, so an in-process episode can wire
the real `LiveMettaGridEpisode` to the real `collab_cooking.coworld.player`
seat objects over a fake websocket. Everything the container smoke exercises
except the container.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from pydantic import ConfigDict, TypeAdapter

from collab_cooking.coworld import lifecycle
from collab_cooking.coworld.live_episode import EpisodeConfig, LiveMettaGridEpisode
from collab_cooking.coworld.native_container import NativeContainer
from collab_cooking.coworld.plan_protocol import NativeProfile, PlayerConfiguration, Registration
from collab_cooking.missions.kitchen import make_kitchen_mission

REGISTRATION = TypeAdapter(Registration, config=ConfigDict(hide_input_in_errors=True))

CERT_CONFIG: dict[str, Any] = {
    "num_agents": 4,
    "layout": "cramped",
    "max_steps": 480,
    "step_seconds": 0.02,
    "policy_action_timeout_seconds": 0.30,
    "plan_interval_steps": 240,
    "player_connect_timeout_seconds": 90,
    "seed": 20260826,
}


class FakeSocket:
    """Native v2 transport fixture using the actual bundled container and parser."""

    def __init__(self, registration: dict[str, Any], endpoint: str) -> None:
        self.registration = registration
        self.endpoint = endpoint
        self.episode: LiveMettaGridEpisode | None = None
        self.connection_id = ""
        self.closed = False
        self.plans: list[dict[str, Any]] = []
        self.final: dict[str, Any] | None = None
        self.actions: list[tuple[int, str]] = []
        self.configured = False
        self.configuration: PlayerConfiguration | None = None
        self.container: NativeContainer | None = None

    async def send_wire(self, raw: str) -> None:
        assert self.episode is not None
        frame = json.loads(raw)
        if frame["type"] == "action":
            pending = self.episode.channels[frame["slot"]].executor.pending
            assert pending is not None
            self.actions.append((pending.step, frame["action_name"]))
        await self.episode.handle_player_message(self.connection_id, frame)

    async def register(self) -> None:
        assert self.configuration is not None
        frozen = REGISTRATION.validate_python({**self.registration, "slot": self.configuration.slot})
        self.container = NativeContainer(self.configuration, frozen, self.endpoint, self.send_wire)
        await self.container.send(frozen)

    async def send_json(self, data: Any) -> None:
        assert not self.closed
        message = json.loads(json.dumps(data, ensure_ascii=False))
        if message["type"] == "player_config":
            self.configuration = PlayerConfiguration.model_validate(message)
            self.configured = True
        elif message["type"] == "final":
            self.final = message
        else:
            assert self.container is not None
            if message["type"] == "plan_installed":
                self.plans.append(message)
            await self.container.receive(json.dumps(message, ensure_ascii=False))

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        assert self.container is not None
        if not await self.container.close_owner(lifecycle.cleanup_deadline()):
            raise lifecycle.OwnershipUnsettled("fixture player did not join")
        self.closed = True


def scripted_registration(baseline: str = "brigade") -> dict[str, Any]:
    return {"type": "register", "kind": "scripted", "baseline": baseline}


def prompt_registration(prompt: str = "Serve as many dishes as you can.") -> dict[str, Any]:
    return {"type": "register", "kind": "native", "prompt": prompt, "profile": NativeProfile().model_dump()}


def build_episode(
    raw_config: dict[str, Any],
    *,
    player_names: list[str] | None = None,
    process_start: float | None = None,
) -> LiveMettaGridEpisode:
    config = EpisodeConfig.from_mapping(raw_config)
    tokens = list(raw_config.get("tokens") or [f"token-{i}" for i in range(config.num_agents)])
    config.num_agents = len(tokens)
    env = make_kitchen_mission(config.layout, config.max_steps, num_agents=config.num_agents)
    return LiveMettaGridEpisode.from_env(
        env,
        config=config,
        tokens=tokens,
        player_names=player_names or [f"policy-{i}" for i in range(config.num_agents)],
        process_start=process_start,
    )


def run_episode(
    raw_config: dict[str, Any],
    registrations: list[dict[str, Any]],
    out_dir: Path,
    *,
    player_names: list[str] | None = None,
    connect: bool = True,
    process_start: float | None = None,
    paused: bool = False,
    run_timeout: float | None = None,
    endpoint: str = "http://127.0.0.1:1",
) -> dict[str, Any]:
    """Run one episode to settlement and write both artifacts into `out_dir`.

    `paused` starts the loop in the state a `{"type":"control","command":
    "pause"}` frame on /global puts it in; `run_timeout` fails the test rather
    than hanging it if the loop never settles.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.json"
    replay_path = out_dir / "replay.json"

    async def go() -> dict[str, Any]:
        episode = build_episode(raw_config, player_names=player_names, process_start=process_start)
        episode.configure_artifacts(
            lambda results, replay: (
                (
                    results_path.write_bytes(results),
                    replay_path.write_bytes(replay),
                )
                and None
            ),
            generated_at="2026-08-25T12:00:00Z",
        )
        sockets: list[FakeSocket] = []
        if connect:
            for slot, registration in enumerate(registrations):
                socket = FakeSocket(registration, endpoint)
                socket.episode = episode
                sockets.append(socket)
                socket.connection_id = await episode.connect_player(slot, socket)
                # The real player sends `register` immediately after the
                # `player_config` frame lands; here the connection id only
                # exists once connect_player returns.
                await socket.register()
        episode.paused = paused
        if run_timeout is None:
            await episode.run()
        else:
            await asyncio.wait_for(episode.run(), timeout=run_timeout)
        return {
            "episode": episode,
            "sockets": sockets,
            "results": json.loads(results_path.read_text(encoding="utf-8")),
            "replay_bytes": replay_path.read_bytes(),
        }

    return asyncio.run(go())


def fast_cert_config(**overrides: Any) -> dict[str, Any]:
    config = dict(CERT_CONFIG)
    config["tokens"] = [f"token-{i}" for i in range(4)]
    config["players"] = [{"name": f"policy-{i}"} for i in range(4)]
    # Tests run the loop as fast as the engine allows; wall-clock pacing is
    # asserted separately by the certification-fixture test.
    config.update(overrides)
    return config


def timed(fn, *args: Any, **kwargs: Any) -> tuple[Any, float]:
    started = time.monotonic()
    value = fn(*args, **kwargs)
    return value, time.monotonic() - started

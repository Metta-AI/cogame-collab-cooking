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

from collab_cooking.coworld.live_episode import EpisodeConfig, LiveMettaGridEpisode
from collab_cooking.coworld.llm import LlmPlanner
from collab_cooking.coworld.player import Seat as PlayerSeat
from collab_cooking.missions.kitchen import make_kitchen_mission

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
    """One player's end of the wire. Runs its brain synchronously."""

    def __init__(self, registration: dict[str, Any]) -> None:
        self.registration = registration
        self.seat = PlayerSeat()
        self.episode: LiveMettaGridEpisode | None = None
        self.connection_id = ""
        self.closed = False
        self.plans: list[dict[str, Any]] = []
        self.final: dict[str, Any] | None = None
        self.actions: list[tuple[int, str]] = []
        self.configured = False

    async def send_json(self, data: Any) -> None:
        if self.closed or self.episode is None:
            return
        # Round-tripping through JSON is the point: it proves the wire frames
        # are serialisable and that nothing leaks a non-JSON object.
        message = json.loads(json.dumps(data, ensure_ascii=False))
        kind = message.get("type")
        if kind == "player_config":
            baseline = self.registration.get("baseline", "brigade")
            self.seat.configure(message, baseline)
            self.configured = True
        elif kind == "observation":
            step = int(message["step"])
            action_name, task = self.seat.act(message.get("observation") or [])
            self.actions.append((step, action_name))
            await self.episode.handle_player_message(
                self.connection_id,
                {
                    "type": "action",
                    "action_name": action_name,
                    "policy_infos": {"policy_name": self.seat.alias, "task": task},
                    "request_id": f"step-{step}",
                },
            )
        elif kind == "plan":
            self.plans.append(message)
            self.seat.apply_plan(message)
        elif kind == "final":
            self.final = message

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = True


def scripted_registration(baseline: str = "brigade") -> dict[str, Any]:
    return {"type": "register", "kind": "scripted", "baseline": baseline}


def prompt_registration(prompt: str = "Serve as many dishes as you can.") -> dict[str, Any]:
    return {"type": "register", "kind": "prompt", "prompt": prompt}


def build_episode(
    raw_config: dict[str, Any],
    *,
    planner: LlmPlanner | None = None,
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
        planner=planner,
        process_start=process_start,
    )


def run_episode(
    raw_config: dict[str, Any],
    registrations: list[dict[str, Any]],
    out_dir: Path,
    *,
    planner: LlmPlanner | None = None,
    player_names: list[str] | None = None,
    connect: bool = True,
    process_start: float | None = None,
) -> dict[str, Any]:
    """Run one episode to settlement and write both artifacts into `out_dir`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.json"
    replay_path = out_dir / "replay.json"

    async def go() -> dict[str, Any]:
        episode = build_episode(
            raw_config, planner=planner, player_names=player_names, process_start=process_start
        )
        episode.configure_artifacts(
            lambda results, replay: (
                results_path.write_bytes(results),
                replay_path.write_bytes(replay),
            )
            and None,
            generated_at="2026-08-25T12:00:00Z",
        )
        sockets: list[FakeSocket] = []
        if connect:
            for slot, registration in enumerate(registrations):
                socket = FakeSocket(registration)
                socket.episode = episode
                sockets.append(socket)
                socket.connection_id = await episode.connect_player(slot, socket)
                # The real player sends `register` immediately after the
                # `player_config` frame lands; here the connection id only
                # exists once connect_player returns.
                await episode.handle_player_message(socket.connection_id, registration)
        await episode.run()
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

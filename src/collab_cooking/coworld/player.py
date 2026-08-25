"""The bundled player: the real policy, in the same image as the game.

One entrypoint, ``/bin/collab-cooking-player``, env-switched:

* ``PLAYER_PROMPT=<standing orders>`` -> registers ``kind:"prompt"``. The game
  runs the LLM (see ``coworld/llm.py``) and sends this seat a ``plan`` message
  at most once per plan turn; the executor here walks the cog there tick by
  tick.
* ``PLAYER_SCRIPTED=brigade|runner|passer|courier`` -> registers
  ``kind:"scripted"`` and plays that baseline all episode.

Neither one ever exits non-zero on a socket problem: the receive loop catches
every connection error and exits 0. The certification runner treats a
non-zero player exit as an episode failure, and the game's own shutdown can
outrun the flushed final frame.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from mettagrid.policy.policy_env_interface import PolicyEnvInterface
from mettagrid.simulator.interface import AgentObservation, ObservationToken

from collab_cooking.agent.brain.policy import (
    BASELINE_NAMES,
    DEFAULT_BASELINE,
    KitchenBrain,
    PlanDirective,
)
from collab_cooking.coworld.plans import PROMPT_RUNES, TALK_RUNES, truncate_runes

EMPTY_LOCATION = 255
# The game container's uvicorn may not be listening yet when this process
# dials: the platform starts the pods together and raw docker starts them back
# to back. A refused connection used to be a silent success (the process caught
# it and exited 0), leaving that seat noop-ing all episode. Bounded: the game's
# own roster wait is `player_connect_timeout_seconds` (90 s in the cert
# fixture, 120 s in the variants), and this stays inside it.
CONNECT_RETRY_SECONDS = 60.0
CONNECT_RETRY_MAX_DELAY = 2.0


def _log(message: str) -> None:
    print(f"collab-cooking player: {message}", file=sys.stderr, flush=True)


def decode_observation(pei: PolicyEnvInterface, raw: list[list[int]], agent_id: int) -> AgentObservation:
    """Raw wire tokens -> the `AgentObservation` the brain's parser reads."""
    by_id = {spec.id: spec for spec in pei.obs_features}
    tokens: list[ObservationToken] = []
    for entry in raw:
        if len(entry) < 3:
            continue
        location, feature_id, value = int(entry[0]), int(entry[1]), int(entry[2])
        if location == EMPTY_LOCATION:
            continue
        spec = by_id.get(feature_id)
        if spec is None:
            continue
        tokens.append(
            ObservationToken(feature=spec, value=value, raw_token=(location, feature_id, value))
        )
    return AgentObservation(agent_id=agent_id, tokens=tokens)


def registration() -> dict[str, Any]:
    prompt = (os.environ.get("PLAYER_PROMPT") or "").strip()
    if prompt:
        return {"type": "register", "kind": "prompt", "prompt": truncate_runes(prompt, PROMPT_RUNES)}
    baseline = (os.environ.get("PLAYER_SCRIPTED") or "").strip().lower()
    if baseline not in BASELINE_NAMES:
        baseline = DEFAULT_BASELINE
    return {"type": "register", "kind": "scripted", "baseline": baseline}


class Seat:
    """One seat's brain, its state, and the plan currently in force."""

    def __init__(self) -> None:
        self.slot = 0
        self.alias = "Cog-A"
        self.layout = "open-kitchen"
        self.pei: PolicyEnvInterface | None = None
        self.brain: KitchenBrain | None = None
        self.state: Any = None
        self.baseline = DEFAULT_BASELINE
        self.aliases: list[str] = []

    def configure(self, message: dict[str, Any], baseline: str) -> None:
        self.slot = int(message.get("slot", 0))
        self.alias = str(message.get("alias", f"Cog-{self.slot}"))
        self.layout = str(message.get("layout", "open-kitchen"))
        self.baseline = baseline
        self.pei = PolicyEnvInterface.model_validate(message["policy_env"])
        self.brain = KitchenBrain(self.pei, self.slot, layout=self.layout, baseline=baseline)
        self.state = self.brain.initial_agent_state()

    def act(self, raw_observation: list[list[int]]) -> tuple[str, str]:
        if self.brain is None or self.pei is None or self.state is None:
            return "noop", "waiting"
        observation = decode_observation(self.pei, raw_observation, self.slot)
        action, self.state = self.brain.step_with_state(observation, self.state)
        return action.name, truncate_runes(action.talk or self.state.current_task, TALK_RUNES)

    def apply_plan(self, message: dict[str, Any]) -> None:
        if self.brain is None or self.state is None:
            return
        station = str(message.get("station", ""))
        if not station:
            # A fallback plan: drop the directive and run the baseline.
            self.brain.apply_plan(self.state, None)
            return
        self.brain.apply_plan(
            self.state,
            PlanDirective(
                turn=int(message.get("turn", 0)),
                station=station,
                recipe=str(message.get("recipe", "any")),
                zone=str(message.get("zone", "any")),
                handoff=str(message.get("handoff", "none")),
                yield_to=str(message.get("yield_to", "none")),
                say=str(message.get("say", "")),
                src=str(message.get("src", "llm")),
            ),
        )


async def connect_with_retry(
    url: str,
    *,
    deadline_seconds: float = CONNECT_RETRY_SECONDS,
    connect: Callable[[], Awaitable[Any]] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], float] = time.monotonic,
) -> Any:
    """Dial the game until it answers, or until the bound runs out."""
    dial = connect or (lambda: websockets.connect(url, max_size=None))
    deadline = now() + deadline_seconds
    delay = 0.25
    while True:
        try:
            return await dial()
        except (OSError, websockets.WebSocketException) as exc:
            if now() >= deadline:
                raise
            _log(f"game not accepting connections yet ({type(exc).__name__}); retrying in {delay:.2f}s")
            await sleep(delay)
            delay = min(CONNECT_RETRY_MAX_DELAY, delay * 2)


async def run_player(url: str) -> None:
    seat = Seat()
    register = registration()
    baseline = register.get("baseline", DEFAULT_BASELINE) if register["kind"] == "scripted" else DEFAULT_BASELINE
    async with await connect_with_retry(url) as websocket:
        async for raw_message in websocket:
            message: dict[str, Any] = json.loads(raw_message)
            kind = message.get("type")
            if kind == "player_config":
                seat.configure(message, baseline)
                await websocket.send(json.dumps(register))
                _log(f"slot {seat.slot} ({seat.alias}) registered as {register['kind']} on {seat.layout}")
            elif kind == "observation":
                step = int(message.get("step", 0))
                action_name, task = seat.act(message.get("observation") or [])
                await websocket.send(
                    json.dumps(
                        {
                            "type": "action",
                            "action_name": action_name,
                            "policy_infos": {"policy_name": seat.alias, "task": task},
                            "request_id": f"step-{step}",
                        }
                    )
                )
            elif kind == "plan":
                seat.apply_plan(message)
            elif kind == "final":
                _log(f"episode finished: reason={message.get('reason')} dishes={message.get('dishes')}")
                return


def main() -> None:
    url = os.environ["COWORLD_PLAYER_WS_URL"]
    try:
        asyncio.run(run_player(url))
    except Exception as exc:  # noqa: BLE001 - a dead socket is not a player failure
        _log(f"socket closed ({type(exc).__name__}: {exc}); exiting 0")
    sys.exit(0)


if __name__ == "__main__":
    main()

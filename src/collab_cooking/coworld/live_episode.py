"""The live episode: the tick loop, the plan turns, and the artifacts.

Ported from the starter's `coworld/live_episode.py`. What is added is the
resolution order the design note specifies, and it is the specification:

1. **Ingest.** For each slot take `latest_policy_actions[slot]`. A seat whose
   latest action does not carry ``request_id == f"step-{step}"`` contributes
   `noop`; a disconnected seat contributes `noop`.
2. **Apply.** `sim.agent(slot).set_action(name)` for all slots, ascending slot.
3. **Step the engine.** `sim.step()`.
4. **Read state.** `grid_objects`, per-cog inventory, `last_action_success`,
   `episode_rewards`.
5. **Derive events** by diffing against the previous tick, in `DIFF_ORDER`.
6. **Record** the tick into the in-memory replay.
7. **Plan boundary.** One parallel batch, dispatched without blocking.
8. **Deliver plans** that landed since the last tick.
9. **Observe.** Send every connected seat its observation, wait for this
   step's actions up to `policy_action_timeout_seconds`, sleep the remainder
   of `step_seconds`.
10. **Deadline guard** at `play_budget_fraction x episode_timeout_seconds`,
    anchored at PROCESS START so the connect wait is inside the budget.
11. **End** at `max_steps` or `sim.is_done()`.
"""

from __future__ import annotations

import asyncio
import itertools
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from mettagrid.config.mettagrid_config import MettaGridConfig
from mettagrid.policy.policy_env_interface import PolicyEnvInterface
from mettagrid.simulator import Simulator
from pydantic import BaseModel, Field

from collab_cooking.agent.brain.entity_map import EntityMap, _is_within_obs_shape
from collab_cooking.agent.brain.obs_parser import ObsParser
from collab_cooking.agent.brain.policy import BASELINE_NAMES, DEFAULT_BASELINE
from collab_cooking.coworld import replay as replay_mod
from collab_cooking.coworld import results as results_mod
from collab_cooking.coworld.llm import (
    LlmPlanner,
    PlanBatch,
    PlanRequest,
    build_system_prompt,
    build_user_message,
)
from collab_cooking.coworld.plans import PROMPT_RUNES, POLICY_NAME_RUNES, truncate_runes
from collab_cooking.game.game import (
    CHOP_MEAT_PROGRESS,
    CHOP_VEG_PROGRESS,
    QUEUE_FRIES,
    QUEUE_SALAD,
    QUEUE_SOUP,
    WASH_PROGRESS,
)
from collab_cooking.kitchens.layouts import (
    LAYOUT_LINES,
    pass_counters,
    reachable_stations,
)

PLAYER_PROTOCOL = "collab-cooking.player.v1"
ALIASES: tuple[str, ...] = ("Cog-A", "Cog-B", "Cog-C", "Cog-D")
REGISTER_GRACE_SECONDS = 5.0
FEED_LINES = 8
RADIO_LINES = 3


class PlayerWebSocket(Protocol):
    async def send_json(self, data: Mapping[str, Any]) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


class PlayerClientMessage(BaseModel):
    type: Literal["action", "takeover", "release_takeover", "register"]
    action_name: str | None = None
    action_index: int | None = None
    policy_infos: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None
    kind: str | None = None
    prompt: str | None = None
    baseline: str | None = None


@dataclass(frozen=True)
class SubmittedAction:
    action_index: int
    action_name: str
    connection_id: str
    policy_infos: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None

    def as_state(self) -> dict[str, Any]:
        return {
            "action_index": self.action_index,
            "action_name": self.action_name,
            "connection_id": self.connection_id,
            "policy_infos": self.policy_infos,
            "request_id": self.request_id,
        }


@dataclass(frozen=True)
class LivePlayerConnection:
    connection_id: str
    slot: int
    websocket: PlayerWebSocket


@dataclass
class Seat:
    slot: int
    alias: str
    name: str
    kind: str = "scripted"
    baseline: str = DEFAULT_BASELINE
    prompt: str = ""
    registered: bool = False
    connected: bool = False
    ever_connected: bool = False
    fallbacks: int = 0
    blocked: int = 0
    handoffs: int = 0
    say: str = ""
    note: str = ""
    last_order: str = "none yet"
    entity_map: EntityMap = field(default_factory=EntityMap)
    # alias -> (position, tick it was last inside THIS seat's window). The
    # game owns both positions, so it can attribute honestly what the seat
    # could actually see; mettagrid publishes an agent's tokens only at its
    # own centre, so the wire observation itself carries no identity.
    ally_memory: dict[str, tuple[tuple[int, int], int, str]] = field(default_factory=dict)
    seen_step: int = 0
    plan_pending: bool = False
    plan_delivered_step: int = -1

    @property
    def seat_kind(self) -> str:
        return "prompt" if self.kind == "prompt" else f"scripted:{self.baseline}"


@dataclass
class EpisodeConfig:
    """The resolved config, exactly as `config_schema` declares it."""

    layout: str = "open-kitchen"
    num_agents: int = 4
    seed: int = 20260825
    max_steps: int = 900
    step_seconds: float = 0.20
    policy_action_timeout_seconds: float = 0.30
    player_connect_timeout_seconds: float = 120.0
    plan_interval_steps: int = 50
    min_plan_interval_seconds: float = 10.0
    plan_timeout_seconds: float = 12.0
    llm_max_requests_per_minute: int = 26
    fallback_scripted: str = DEFAULT_BASELINE
    play_budget_fraction: float = 0.6
    episode_timeout_seconds: float = 1200.0
    shutdown_grace_seconds: float = 20.0
    ticket_interarrival: int = 18
    ticket_deadline: int = 50
    order_queue_max: int = 8
    chop_ticks: int = 3
    wash_ticks: int = 3
    soup_cook_ticks: int = 10
    soup_burn_ticks: int = 14
    fries_cook_ticks: int = 8
    fries_burn_ticks: int = 11
    model: str = "claude-haiku-4-5-20251001"
    max_output_tokens: int = 900

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> EpisodeConfig:
        """Coerce the episode config, ignoring keys this game does not own."""
        defaults = cls()
        kwargs: dict[str, Any] = {}
        for key, value in raw.items():
            if key not in cls.__dataclass_fields__ or value is None:
                continue
            current = getattr(defaults, key)
            if isinstance(current, bool):
                kwargs[key] = bool(value)
            elif isinstance(current, int):
                kwargs[key] = int(value)
            elif isinstance(current, float):
                kwargs[key] = float(value)
            else:
                kwargs[key] = str(value)
        return cls(**kwargs)

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def assign_aliases(seed: int, num_agents: int) -> list[str]:
    """`Cog-A`..`Cog-D` by a seeded permutation of slots.

    A policy cannot infer "slot 0 is always the strongest entrant", and the
    wire observation carries no names at all, so scripted seats are anonymous
    by construction.
    """
    pool = list(ALIASES[:num_agents])
    random.Random(seed).shuffle(pool)
    return pool


class LiveMettaGridEpisode:
    def __init__(
        self,
        sim: Any,
        policy_env: PolicyEnvInterface,
        *,
        config: EpisodeConfig,
        tokens: list[str],
        player_names: list[str],
        planner: LlmPlanner | None = None,
        process_start: float | None = None,
        disconnect_exception_types: tuple[type[Exception], ...] = (RuntimeError,),
        request_shutdown: Callable[[], None] = lambda: None,
    ) -> None:
        self.sim = sim
        self.policy_env = policy_env
        self.config = config
        self.tokens = list(tokens)
        self.process_start = process_start if process_start is not None else time.monotonic()
        self.disconnect_exception_types = disconnect_exception_types
        self.request_shutdown = request_shutdown

        self.action_names = list(policy_env.action_names)
        self.noop_action_index = self.action_names.index("noop")
        self._noop_action = SubmittedAction(
            action_index=self.noop_action_index,
            action_name="noop",
            connection_id="system",
        )
        self.latest_policy_actions = [self._noop_action for _ in self.tokens]
        self.latest_action_indices = [self.noop_action_index for _ in self.tokens]
        self.applied_actions = ["noop" for _ in self.tokens]

        aliases = assign_aliases(config.seed, len(self.tokens))
        self.seats = [
            Seat(
                slot=slot,
                alias=aliases[slot],
                name=truncate_runes(
                    player_names[slot] if slot < len(player_names) else f"seat-{slot}", POLICY_NAME_RUNES
                ),
                baseline=config.fallback_scripted,
            )
            for slot in range(len(self.tokens))
        ]
        self.alias_to_slot = {seat.alias: seat.slot for seat in self.seats}

        self.connections: dict[str, LivePlayerConnection] = {}
        self.connections_by_slot: dict[int, dict[str, LivePlayerConnection]] = {
            slot: {} for slot in range(len(self.tokens))
        }
        self._connection_ids = (f"player-{idx}" for idx in itertools.count())
        self._policy_action_event = asyncio.Event()

        self.play_task: asyncio.Task[None] | None = None
        self.done = False
        self.exited = False
        self.paused = False
        self.reason = ""
        self.step_seconds = config.step_seconds

        self.planner = planner
        self.batch: PlanBatch | None = None
        self.turn = 0
        self.last_batch_at = 0.0
        self.total_turns = max(1, config.max_steps // max(1, config.plan_interval_steps))

        self._obs_parser = ObsParser(policy_env)
        self._pass_counters = pass_counters(config.layout)
        self.heat: dict[tuple[int, int], int] = {}
        self.ticket_expiries = replay_mod.ticket_expiries(config.max_steps)
        self.feed: list[dict[str, Any]] = []
        self.beats: list[dict[str, Any]] = []
        self.ticker: list[dict[str, Any]] = []
        self.served_by_recipe: dict[str, int] = {"salad": 0, "soup": 0, "fries": 0}
        self.orders_arrived = 0
        self.orders_expired = 0
        self.burned = {"pot": 0, "fryer": 0}
        self.state = replay_mod.TickState(step=0, cogs=[], delivered=[0] * len(self.tokens))

        self.writer: replay_mod.ReplayWriter | None = None
        self._artifact_sink: Callable[[bytes, bytes], None] | None = None
        self._pending_events: list[dict[str, Any]] = []

    # -- wiring -------------------------------------------------------------
    @classmethod
    def from_env(
        cls,
        env: MettaGridConfig,
        *,
        config: EpisodeConfig,
        tokens: list[str],
        player_names: list[str],
        planner: LlmPlanner | None = None,
        process_start: float | None = None,
        disconnect_exception_types: tuple[type[Exception], ...] = (RuntimeError,),
        request_shutdown: Callable[[], None] = lambda: None,
    ) -> LiveMettaGridEpisode:
        sim = Simulator().new_simulation(env, seed=config.seed)
        return cls(
            sim,
            PolicyEnvInterface.from_mg_cfg(sim.config),
            config=config,
            tokens=tokens,
            player_names=player_names,
            planner=planner,
            process_start=process_start,
            disconnect_exception_types=disconnect_exception_types,
            request_shutdown=request_shutdown,
        )

    def configure_artifacts(self, sink: Callable[[bytes, bytes], None], *, generated_at: str) -> None:
        self._artifact_sink = sink
        config = self.config.as_dict()
        self.writer = replay_mod.ReplayWriter(
            layout=self.config.layout,
            seed=self.config.seed,
            config=config,
            seats=[self.seat_block(seat) for seat in self.seats],
            generated_at=generated_at,
        )

    def seat_block(self, seat: Seat) -> dict[str, Any]:
        return {
            "slot": seat.slot,
            "alias": seat.alias,
            "name": seat.name,
            "kind": seat.kind,
            "baseline": "" if seat.kind == "prompt" else seat.baseline,
            "color": seat.slot,
            "disconnected": not seat.ever_connected,
        }

    # -- connections --------------------------------------------------------
    async def connect_player(self, slot: int, websocket: PlayerWebSocket) -> str:
        connection_id = next(self._connection_ids)
        connection = LivePlayerConnection(connection_id=connection_id, slot=slot, websocket=websocket)
        await websocket.send_json(self.player_config_message(slot, connection_id))
        self.connections[connection_id] = connection
        self.connections_by_slot[slot][connection_id] = connection
        seat = self.seats[slot]
        seat.connected = True
        seat.ever_connected = True
        return connection_id

    def disconnect_player(self, connection_id: str) -> None:
        connection = self.connections.pop(connection_id, None)
        if connection is None:
            return
        self.connections_by_slot[connection.slot].pop(connection_id, None)
        if not self.connections_by_slot[connection.slot]:
            self.seats[connection.slot].connected = False
        self._policy_action_event.set()

    def connected_slots(self) -> set[int]:
        return {connection.slot for connection in self.connections.values()}

    async def handle_player_message(self, connection_id: str, raw_message: Mapping[str, Any]) -> None:
        connection = self.connections.get(connection_id)
        if connection is None:
            return
        try:
            message = PlayerClientMessage.model_validate(raw_message)
        except Exception:  # noqa: BLE001 - a malformed frame is never a disconnect
            return
        seat = self.seats[connection.slot]
        if message.type == "register":
            self._register(seat, message)
            return
        if message.type in ("takeover", "release_takeover"):
            await connection.websocket.send_json(self.player_config_message(connection.slot, connection_id))
            return
        self.latest_policy_actions[connection.slot] = self._submitted_action(connection_id, message)
        self._policy_action_event.set()

    def _register(self, seat: Seat, message: PlayerClientMessage) -> None:
        """An unknown baseline, a malformed frame, or no registration within
        5 s of connect is treated as `{"kind":"scripted","baseline":"brigade"}`
        -- never a disconnect."""
        seat.registered = True
        kind = (message.kind or "").strip().lower()
        if kind == "prompt":
            seat.kind = "prompt"
            seat.prompt = truncate_runes(message.prompt, PROMPT_RUNES)
            return
        seat.kind = "scripted"
        baseline = (message.baseline or "").strip().lower()
        seat.baseline = baseline if baseline in BASELINE_NAMES else self.config.fallback_scripted

    # -- the loop -----------------------------------------------------------
    async def run(self) -> None:
        await self._wait_for_roster()
        if not self.connected_slots():
            await self._settle("no_players")
            return
        await self._wait_for_registrations()
        self._push_event({"ev": "episode_start", "layout": self.config.layout})
        self.state = replay_mod.capture(self.sim, len(self.tokens))
        await self._send_observations()
        while self.sim.current_step < self.config.max_steps and not self.sim.is_done():
            if self.paused:
                # The guard is NOT paused. `pause` is a spectator control that
                # anything reaching WS /global can send, and a paused loop
                # advances no step, so without this the episode would sit here
                # until the platform killed it, with no artifacts and no exit.
                if self._deadline_reached():  # 10
                    await self._settle("deadline")
                    return
                await asyncio.sleep(0.05)
                continue
            step = int(self.sim.current_step)
            tick_started = time.monotonic()

            await self._wait_for_policy_actions(step)
            self._apply_actions(step)  # 1 + 2
            self.sim.step()  # 3
            current = replay_mod.capture(self.sim, len(self.tokens))  # 4
            events = replay_mod.derive_events(  # 5
                self.state, current, self.applied_actions, [s.alias for s in self.seats], self.heat
            )
            self._absorb(events)
            self._record(current, events)  # 6
            self.state = current
            self._update_prompt_memory()
            self._plan_boundary(step)  # 7
            await self._deliver_plans()  # 8
            await self._send_observations()  # 9
            elapsed = time.monotonic() - tick_started
            if elapsed < self.step_seconds:
                await asyncio.sleep(self.step_seconds - elapsed)
            if self._deadline_reached():  # 10
                await self._settle("deadline")
                return
        await self._settle("complete")  # 11

    def _deadline_reached(self) -> bool:
        budget = self.config.play_budget_fraction * self.config.episode_timeout_seconds
        return (time.monotonic() - self.process_start) >= budget

    async def _wait_for_roster(self) -> None:
        """Bounded. Start the moment everyone is here, else at the deadline."""
        deadline = self.process_start + self.config.player_connect_timeout_seconds
        while len(self.connected_slots()) < len(self.tokens):
            if time.monotonic() >= deadline or self._deadline_reached():
                return
            await asyncio.sleep(0.05)

    async def _wait_for_registrations(self) -> None:
        deadline = time.monotonic() + REGISTER_GRACE_SECONDS
        while time.monotonic() < deadline:
            if all(self.seats[slot].registered for slot in self.connected_slots()):
                break
            await asyncio.sleep(0.02)
        for seat in self.seats:
            if not seat.registered:
                seat.kind = "scripted"
                seat.baseline = self.config.fallback_scripted
        if self.writer is not None:
            self.writer.seats = [self.seat_block(seat) for seat in self.seats]

    async def _wait_for_policy_actions(self, step: int) -> None:
        deadline = asyncio.get_running_loop().time() + self.config.policy_action_timeout_seconds
        while not self._policy_actions_ready(step):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            self._policy_action_event.clear()
            if self._policy_actions_ready(step):
                return
            try:
                await asyncio.wait_for(self._policy_action_event.wait(), timeout=remaining)
            except TimeoutError:
                return

    def _policy_actions_ready(self, step: int) -> bool:
        expected = f"step-{step}"
        return all(
            not self.connections_by_slot[slot] or self.latest_policy_actions[slot].request_id == expected
            for slot in range(len(self.tokens))
        )

    def _apply_actions(self, step: int) -> None:
        expected = f"step-{step}"
        for slot in range(len(self.tokens)):
            action = self.latest_policy_actions[slot]
            if action.request_id != expected or not self.connections_by_slot[slot]:
                action = self._noop_action
            self.latest_action_indices[slot] = action.action_index
            self.applied_actions[slot] = action.action_name
            self.sim.agent(slot).set_action(action.action_name)

    def _record(self, current: replay_mod.TickState, events: list[dict[str, Any]]) -> None:
        if self.writer is None:
            return
        flags = []
        for slot, seat in enumerate(self.seats):
            bits = 0
            if slot < len(current.cogs) and not current.cogs[slot]["success"]:
                if self.applied_actions[slot].startswith("move_"):
                    bits |= 1
            if seat.plan_delivered_step == current.step:
                bits |= 2
            if not seat.connected:
                bits |= 4
            flags.append(bits)
        if self._pending_events:
            events = [*self._pending_events, *events]
            self._pending_events.clear()
        self.writer.append(current, list(self.applied_actions), flags, events)
        self.writer.heat = self.heat

    def _absorb(self, events: list[dict[str, Any]]) -> None:
        """Fold derived events into the counters, the feed and the beats."""
        for event in events:
            kind = event["ev"]
            if kind == "order_arrive":
                self.orders_arrived += 1
            elif kind == "order_expire":
                self.orders_expired += 1
                self._feed(event, f"ticket {event['recipe']} expires - nobody served it", "expire")
                self._beat(event, "expire", f"Ticket {event['recipe']} expires")
            elif kind == "pot_burn":
                self.burned["pot"] += 1
                self._feed(event, "the pot burns - nobody plated it", "burn")
                self._beat(event, "burn", "Pot burns")
            elif kind == "fry_burn":
                self.burned["fryer"] += 1
                self._feed(event, "the fryer burns - nobody plated it", "burn")
                self._beat(event, "burn", "Fryer burns")
            elif kind == "serve":
                recipe = event.get("recipe", "salad")
                self.served_by_recipe[recipe] = self.served_by_recipe.get(recipe, 0) + 1
                dish = event.get("dish", 0)
                self.ticker.append({"t": event.get("t", self.sim.current_step), "recipe": recipe, "alias": event["alias"]})
                self._feed(event, f"{event['alias']} serves {recipe} - dish {dish}", "serve")
                self._beat(event, "serve", f"Dish {dish} - {event['alias']} serves {recipe}")
            elif kind == "blocked":
                slot = event["slot"]
                self.seats[slot].blocked += 1
            elif kind == "deposit":
                slot = event["slot"]
                if (event["y"], event["x"]) in self._pass_counters or self._near_pass(event):
                    self.seats[slot].handoffs += 1
                    self._feed(
                        event,
                        f"{event['alias']} leaves {event['item'].replace('_', ' ')} on the counter",
                        "handoff",
                    )

    def _near_pass(self, event: dict[str, Any]) -> bool:
        here = (event["y"], event["x"])
        return any(
            (here[0] + dr, here[1] + dc) in self._pass_counters
            for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1))
        )

    def _feed(self, event: dict[str, Any], text: str, kind: str) -> None:
        self.feed.append({"t": int(self.sim.current_step), "kind": kind, "text": truncate_runes(text, 120)})
        if len(self.feed) > 400:
            del self.feed[:-400]

    def _beat(self, event: dict[str, Any], kind: str, label: str) -> None:
        self.beats.append({"t": int(self.sim.current_step), "k": kind, "label": truncate_runes(label, 60)})

    def _push_event(self, event: dict[str, Any]) -> None:
        """Attach an out-of-band event to the tick record it belongs to.

        `episode_start` fires before any tick exists, so it is buffered and
        rides the first record; everything else lands on the tick just
        written.
        """
        if self.writer is None:
            return
        if not self.writer.ticks:
            self._pending_events.append(event)
            return
        self.writer.ticks[-1].setdefault("ev", []).append(event)

    # -- plan turns ---------------------------------------------------------
    def _prompt_seats(self) -> list[Seat]:
        return [seat for seat in self.seats if seat.kind == "prompt" and seat.connected]

    def _plan_boundary(self, step: int) -> None:
        if self.planner is None or self.planner.disabled:
            self._fallback_all("disabled", step)
            return
        if step % self.config.plan_interval_steps != 0:
            return
        if self.batch is not None and not self.batch.finished:
            return
        now = time.monotonic()
        if self.last_batch_at and now - self.last_batch_at < self.config.min_plan_interval_seconds:
            return
        seats = self._prompt_seats()
        if not seats:
            return
        self.turn += 1
        self.last_batch_at = now
        requests = [self._plan_request(seat) for seat in seats]
        for seat in seats:
            seat.plan_pending = True
        self.batch = self.planner.start_turn(self.turn, requests)

    def _fallback_all(self, cause: str, step: int) -> None:
        """No LLM at all: every prompt seat plays its baseline all episode."""
        if step % self.config.plan_interval_steps != 0:
            return
        seats = self._prompt_seats()
        if not seats:
            return
        self.turn += 1
        for seat in seats:
            seat.fallbacks += 1
            self._push_event({"ev": "fallback", "slot": seat.slot, "alias": seat.alias, "cause": cause})

    def _plan_request(self, seat: Seat) -> PlanRequest:
        view = self._seat_view(seat)
        system = build_system_prompt(
            seat.alias,
            self.config.max_steps,
            self.config.layout,
            LAYOUT_LINES.get(self.config.layout, ""),
            self.config.plan_interval_steps,
            seat.prompt,
        )
        return PlanRequest(
            slot=seat.slot,
            alias=seat.alias,
            system=system,
            user=build_user_message(view),
            legal_stations=view["legal_stations"],
            ally_aliases=[s.alias for s in self.seats if s.slot != seat.slot] + ["none"],
        )

    def _seat_view(self, seat: Seat) -> dict[str, Any]:
        """Render this seat's OWN window into the prompt's observation view."""
        self._refresh_entity_map(seat)
        emap = seat.entity_map
        step = int(self.sim.current_step)
        position = self.state.cogs[seat.slot]["pos"] if self.state.cogs else (0, 0)
        legal = reachable_stations(self.config.layout, position)

        team = []
        for other in self.seats:
            if other.slot == seat.slot:
                continue
            entry: dict[str, Any] = {"alias": other.alias, "pos": None, "age": 0, "seen": None, "carrying": ""}
            remembered = seat.ally_memory.get(other.alias)
            if remembered is not None:
                pos, seen, carrying = remembered
                entry["pos"] = pos
                entry["seen"] = seen
                entry["age"] = max(0, step - seen)
                entry["carrying"] = carrying
            team.append(entry)

        board_entities = emap.find("order_board")
        board_inv: dict[str, int] = {}
        board_age: int | None = None
        if board_entities:
            board_entities.sort(key=lambda pair: pair[1].last_seen, reverse=True)
            board_inv = board_entities[0][1].properties
            board_age = max(0, seat.seen_step - board_entities[0][1].last_seen)

        station_rows = []
        for kind, label in (
            ("chopping_station", "chopping"),
            ("cooking_station", "pot"),
            ("fryer_station", "fryer"),
            ("serving_station", "pass"),
            ("wash_station", "sink"),
            ("veg_station", "veg"),
            ("meat_station", "meat"),
            ("plate_station", "plates"),
        ):
            found = emap.find(kind)
            if not found:
                continue
            found.sort(key=lambda pair: pair[1].last_seen, reverse=True)
            pos, entity = found[0]
            station_rows.append(
                {
                    "name": label,
                    "pos": pos,
                    "age": max(0, seat.seen_step - entity.last_seen),
                    "note": _station_note(kind, entity.properties),
                }
            )

        counters = []
        for pos, entity in emap.find("wall"):
            item = replay_mod.carried_item(entity.properties)
            if item:
                counters.append((pos, item))
        counters.sort()

        blocked_at = max(self.heat, key=lambda key: self.heat[key], default=None)
        radio = [
            (other.alias, other.say)
            for other in self.seats
            if other.slot != seat.slot and other.say
        ][:RADIO_LINES]
        return {
            "turn": self.turn + 1,
            "turns": self.total_turns,
            "tick": step,
            "ticks": self.config.max_steps,
            "layout": self.config.layout,
            "dishes": sum(self.state.delivered),
            "live": len(
                [t for t in self.state.board.items() if t[0].startswith("ticket_") and t[1] > 0]
            ),
            "alias": seat.alias,
            "position": position,
            "carrying": self.state.cogs[seat.slot]["carrying"] if self.state.cogs else "",
            "delivered": self.state.delivered[seat.slot] if self.state.delivered else 0,
            "team": team,
            "board": {
                "age": board_age,
                "salad": board_inv.get(QUEUE_SALAD, 0),
                "soup": board_inv.get(QUEUE_SOUP, 0),
                "fries": board_inv.get(QUEUE_FRIES, 0),
            },
            "stations": station_rows,
            "counters": counters,
            "blocked": seat.blocked,
            "blocked_at": (blocked_at[1], blocked_at[0]) if blocked_at else None,
            "legal_stations": legal,
            "last_order": seat.last_order,
            "radio": radio,
            "note": seat.note,
        }

    def _update_prompt_memory(self) -> None:
        """Keep every prompt seat's remembered world model current.

        Only prompt seats pay for this; with no credentials there are none, so
        an all-scripted episode does no extra work at all.
        """
        for seat in self.seats:
            if seat.kind != "prompt":
                continue
            self._refresh_entity_map(seat)
            self._refresh_allies(seat)

    def _refresh_allies(self, seat: Seat) -> None:
        if seat.slot >= len(self.state.cogs):
            return
        here = self.state.cogs[seat.slot]["pos"]
        step = int(self.sim.current_step)
        half_h, half_w = self._obs_parser.obs_half_h, self._obs_parser.obs_half_w
        for other in self.seats:
            if other.slot == seat.slot or other.slot >= len(self.state.cogs):
                continue
            there = self.state.cogs[other.slot]["pos"]
            if _is_within_obs_shape(there[0] - here[0], there[1] - here[1], half_h, half_w):
                seat.ally_memory[other.alias] = (
                    there,
                    step,
                    self.state.cogs[other.slot]["carrying"],
                )

    def _refresh_entity_map(self, seat: Seat) -> None:
        try:
            observation = self.sim.agent(seat.slot).observation
        except Exception:  # noqa: BLE001 - never strand the tick loop
            return
        position = self.state.cogs[seat.slot]["pos"] if self.state.cogs else (0, 0)
        parsed, visible = self._obs_parser.parse(observation, fallback_position=position)
        seat.seen_step = int(self.sim.current_step)
        seat.entity_map.update_from_observation(
            parsed.position,
            self._obs_parser.obs_half_h,
            self._obs_parser.obs_half_w,
            visible,
            seat.seen_step,
        )

    async def _deliver_plans(self) -> None:
        if self.batch is None:
            return
        for outcome in self.batch.poll():
            seat = self.seats[outcome.slot]
            seat.plan_pending = False
            if outcome.ok and outcome.plan is not None:
                plan = outcome.plan
                seat.say = plan.say
                seat.note = plan.note
                seat.last_order = (
                    f"station={plan.station} recipe={plan.recipe} zone={plan.zone} handoff={plan.handoff}"
                )
                event = plan.replay_event(seat.slot, seat.alias, self.batch.turn, outcome.src)
                self._push_event(event)
                if plan.say:
                    self._feed(event, f"{seat.alias}: {plan.say}", "say")
                self._beat({}, "plan", f"{seat.alias} takes {plan.station}")
                await self._send_to_slot(
                    seat.slot,
                    {
                        "type": "plan",
                        "protocol": PLAYER_PROTOCOL,
                        "turn": self.batch.turn,
                        "step": int(self.sim.current_step),
                        "station": plan.station,
                        "recipe": plan.recipe,
                        "zone": plan.zone,
                        "handoff": plan.handoff,
                        "yield_to": plan.yield_to,
                        "say": plan.say,
                        "note": plan.note,
                        "src": outcome.src,
                    },
                )
                seat.plan_delivered_step = int(self.sim.current_step)
            else:
                seat.fallbacks += 1
                self._push_event(
                    {"ev": "fallback", "slot": seat.slot, "alias": seat.alias, "cause": outcome.cause}
                )
                self._feed(
                    {},
                    f"{seat.alias} fell back to {self.config.fallback_scripted} - {outcome.cause}",
                    "fallback",
                )
                await self._send_to_slot(
                    seat.slot,
                    {
                        "type": "plan",
                        "protocol": PLAYER_PROTOCOL,
                        "turn": self.batch.turn,
                        "step": int(self.sim.current_step),
                        "station": "",
                        "recipe": "any",
                        "zone": "any",
                        "handoff": "none",
                        "yield_to": "none",
                        "say": "",
                        "note": "",
                        "src": outcome.src,
                    },
                )

    # -- messages -----------------------------------------------------------
    async def _send_observations(self) -> None:
        await self._send_to_players(
            {
                connection_id: self.observation_message(connection.slot)
                for connection_id, connection in self.connections.items()
            }
        )

    async def _send_to_slot(self, slot: int, message: dict[str, Any]) -> None:
        await self._send_to_players(
            {connection_id: message for connection_id in self.connections_by_slot[slot]}
        )

    async def _send_to_players(self, messages: dict[str, dict[str, Any]]) -> None:
        connections = tuple(
            (connection_id, self.connections[connection_id])
            for connection_id in messages
            if connection_id in self.connections
        )
        if not connections:
            return
        results = await asyncio.gather(
            *(connection.websocket.send_json(messages[cid]) for cid, connection in connections),
            return_exceptions=True,
        )
        for (connection_id, _connection), result in zip(connections, results, strict=True):
            if isinstance(result, Exception):
                self.disconnect_player(connection_id)

    def player_config_message(self, slot: int, connection_id: str) -> dict[str, Any]:
        return {
            "game": results_mod.GAME_NAME,
            "type": "player_config",
            "protocol": PLAYER_PROTOCOL,
            "slot": slot,
            "connection_id": connection_id,
            "num_agents": len(self.tokens),
            "action_names": self.action_names,
            "observation_shape": list(self.policy_env.observation_shape),
            "policy_env": self.policy_env.model_dump(mode="json"),
            "observation": self.observation_metadata(),
            "control_state": self.slot_control_state(slot),
            "alias": self.seats[slot].alias,
            "layout": self.config.layout,
            "max_steps": self.config.max_steps,
        }

    def observation_message(self, slot: int) -> dict[str, Any]:
        return {
            "game": results_mod.GAME_NAME,
            "type": "observation",
            "protocol": PLAYER_PROTOCOL,
            "slot": slot,
            "step": int(self.sim.current_step),
            "observation": self.sim._c_sim.observations()[slot].tolist(),
            "scores": self.scores(),
            "control_state": self.slot_control_state(slot),
        }

    def observation_metadata(self) -> dict[str, Any]:
        return {
            "width": self.policy_env.obs_width,
            "height": self.policy_env.obs_height,
            "features": [
                {"id": feature.id, "name": feature.name, "normalization": feature.normalization}
                for feature in self.policy_env.obs_features
            ],
            "tags": self.policy_env.tags,
            "global_location": 254,
            "empty_location": 255,
        }

    def slot_control_state(self, slot: int) -> dict[str, Any]:
        return {"control_mode": "policy", "tick_mode": "fixed"}

    def scores(self) -> list[float]:
        return [float(score) for score in self.sim.episode_rewards.tolist()]

    def delivered(self) -> list[int]:
        return [int(round(score)) for score in self.sim.episode_rewards.tolist()]

    # -- settle -------------------------------------------------------------
    def results(self) -> dict[str, Any]:
        delivered = self.delivered() if self.reason != "no_players" else [0] * len(self.tokens)
        return results_mod.build_results(
            reason=self.reason,
            layout=self.config.layout,
            steps=int(self.sim.current_step),
            delivered=delivered,
            served_by_recipe=self.served_by_recipe,
            orders_arrived=self.orders_arrived,
            orders_expired=self.orders_expired,
            burned=self.burned,
            blocked_moves=[seat.blocked for seat in self.seats],
            handoffs=[seat.handoffs for seat in self.seats],
            names=[seat.name for seat in self.seats],
            aliases=[seat.alias for seat in self.seats],
            seat_kinds=[seat.seat_kind for seat in self.seats],
            disconnected=[not seat.ever_connected for seat in self.seats],
            fallbacks=[seat.fallbacks for seat in self.seats],
            llm_requests=self.planner.requests if self.planner else 0,
        )

    async def _settle(self, reason: str) -> None:
        self.reason = reason
        self.done = True
        if reason == "deadline":
            self._push_event({"ev": "deadline", "step": int(self.sim.current_step)})
        self._push_event({"ev": "episode_end", "reason": reason})
        results = self.results()
        if self.writer is not None:
            self.writer.seats = [self.seat_block(seat) for seat in self.seats]
            self.writer.heat = self.heat
        if self._artifact_sink is not None and self.writer is not None:
            self._artifact_sink(results_mod.encode(results), self.writer.encode(results))
        await self._send_final(results)
        if self.planner is not None:
            self.planner.shutdown()
        # The certification runner pings /global AFTER the player pods start,
        # and a fast exit fails the episode: keep answering for the grace.
        await asyncio.sleep(self.config.shutdown_grace_seconds)
        self.exited = True
        self.request_shutdown()

    async def _send_final(self, results: dict[str, Any]) -> None:
        message = {
            **self.snapshot(),
            "type": "final",
            "done": True,
            "reason": self.reason,
            "scores": results["scores"],
            "dishes": results["dishes"],
            "names": results["names"],
            "aliases": results["aliases"],
        }
        await self._send_to_players({connection_id: message for connection_id in self.connections})

    # -- spectator ----------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        cogs = [
            {
                "slot": seat.slot,
                "alias": seat.alias,
                "name": seat.name,
                "kind": seat.kind,
                "x": self.state.cogs[seat.slot]["pos"][1] if seat.slot < len(self.state.cogs) else 0,
                "y": self.state.cogs[seat.slot]["pos"][0] if seat.slot < len(self.state.cogs) else 0,
                "carrying": self.state.cogs[seat.slot]["carrying"] if seat.slot < len(self.state.cogs) else "",
                "say": seat.say,
                "connected": seat.connected,
            }
            for seat in self.seats
        ]
        return {
            "game": results_mod.GAME_NAME,
            "type": "state",
            "step": int(self.sim.current_step),
            "max_steps": self.config.max_steps,
            "layout": self.config.layout,
            "dishes": sum(self.delivered()),
            "scores": results_mod.seat_scores(self.delivered()),
            "delivered": self.delivered(),
            "aliases": [seat.alias for seat in self.seats],
            "player_names": [seat.name for seat in self.seats],
            "connected": [seat.connected for seat in self.seats],
            "paused": self.paused,
            "done": self.done,
            "reason": self.reason,
            "stations": replay_mod.station_summary(self.state, self.ticket_expiries),
            "cogs": cogs,
            "feed": self.feed[-FEED_LINES:],
        }

    # -- helpers ------------------------------------------------------------
    def _submitted_action(self, connection_id: str, message: PlayerClientMessage) -> SubmittedAction:
        index = self._action_index(message)
        return SubmittedAction(
            action_index=index,
            action_name=self.action_names[index],
            connection_id=connection_id,
            policy_infos=message.policy_infos,
            request_id=message.request_id,
        )

    def _action_index(self, message: PlayerClientMessage) -> int:
        if message.action_name is not None:
            if message.action_name in self.action_names:
                return self.action_names.index(message.action_name)
            return self.noop_action_index
        if message.action_index is not None and 0 <= message.action_index < len(self.action_names):
            return message.action_index
        return self.noop_action_index


def _station_note(kind: str, properties: dict[str, int]) -> str:
    if kind == "chopping_station":
        veg = properties.get(CHOP_VEG_PROGRESS, 0)
        meat = properties.get(CHOP_MEAT_PROGRESS, 0)
        if veg:
            return f"veg {veg}/3"
        if meat:
            return f"meat {meat}/3"
        return "empty"
    if kind == "cooking_station":
        return replay_mod.pot_state(properties)
    if kind == "fryer_station":
        return replay_mod.fryer_state(properties)
    if kind == "wash_station":
        wash = properties.get(WASH_PROGRESS, 0)
        return f"washing {wash}/3" if wash else "idle"
    return ""

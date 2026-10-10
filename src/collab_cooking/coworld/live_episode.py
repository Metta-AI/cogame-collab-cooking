"""The live episode: the tick loop, the plan turns, and the artifacts.

Ported from the starter's `coworld/live_episode.py`. What is added is the
resolution order the design note specifies, and it is the specification:

1. **Ingest.** Admit one physical action against its immutable private observation
   UUID and independently mirrored controller. Missing or mismatched controls
   contribute `noop`; external research actions retain their separate provenance.
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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from mettagrid.config.mettagrid_config import MettaGridConfig
from mettagrid.policy.policy_env_interface import PolicyEnvInterface
from mettagrid.simulator import Simulator
from pydantic import ValidationError

from collab_cooking.agent.brain.entity_map import EntityMap
from collab_cooking.agent.brain.obs_parser import ObsParser
from collab_cooking.agent.brain.policy import DEFAULT_BASELINE
from collab_cooking.coworld import lifecycle
from collab_cooking.coworld import replay as replay_mod
from collab_cooking.coworld import results as results_mod
from collab_cooking.coworld.decision_view import KitchenView
from collab_cooking.coworld.llm import build_system_prompt, build_user_message
from collab_cooking.coworld.plan_admission import PlanAdmission, PlayerRoster
from collab_cooking.coworld.plan_channel import PLAYER_FRAME, PlanChannel
from collab_cooking.coworld.plan_protocol import (
    AttemptProgress,
    DirectiveInstalled,
    NativeProfile,
    NativeRegistration,
    PhysicalObservation,
    PlanWindow,
    PlayerConfiguration,
    ScriptedRegistration,
)
from collab_cooking.coworld.plans import ALIAS_CAP, FEED_RUNES, POLICY_NAME_RUNES, truncate_runes
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

from .private_journal import (
    EngineApplication,
    EngineOutcome,
    GrantedCall,
    InstalledMacro,
    Journal,
    OwnershipState,
    PrivateTick,
    PrivateWindow,
    ReceivedCall,
    RegisteredPolicies,
)

ALIASES: tuple[str, ...] = ("Cog-A", "Cog-B", "Cog-C", "Cog-D")
REGISTER_GRACE_SECONDS = 5.0
FEED_LINES = 8
RADIO_LINES = 3


class PlayerWebSocket(Protocol):
    async def send_json(self, data: Mapping[str, Any]) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


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
    action_fallbacks: dict[str, int] = field(
        default_factory=lambda: {"invalid_action": 0, "deadline": 0, "disconnected": 0, "malformed_frame": 0}
    )
    blocked: int = 0
    handoffs: int = 0
    say: str = ""
    note: str = ""
    last_order: str = "none yet"
    entity_map: EntityMap = field(default_factory=EntityMap)
    # alias -> (position, tick it was last inside THIS seat's window). The
    # game owns both positions, so it can attribute honestly what the seat
    # could actually see. Inventory comes only from the visible agent tokens;
    # source-owned seat positions attribute the observed tile to its alias.
    ally_memory: dict[str, tuple[tuple[int, int], int, str]] = field(default_factory=dict)
    seen_step: int = 0
    plan_pending: bool = False
    plan_delivered_step: int = -1
    private_heat: dict[tuple[int, int], int] = field(default_factory=dict)

    @property
    def seat_kind(self) -> str:
        if self.kind == "scripted":
            return f"scripted:{self.baseline}"
        return self.kind


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
        process_start: float | None = None,
        disconnect_exception_types: tuple[type[Exception], ...] = (RuntimeError,),
        request_shutdown: Callable[[], None] = lambda: None,
        evidence_profile: Literal["native_runtime", "scripted_teacher_logical_clock_v1"] = "native_runtime",
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
        self.physics_task: asyncio.Task[None] | None = None
        self.player_receivers: set[asyncio.Task] = set()
        self.done = False
        self.closing = False
        self.exited = False
        self.paused = False
        self.reason = ""
        self.step_seconds = config.step_seconds

        self.roster = PlayerRoster()
        self.admission = PlanAdmission(config.llm_max_requests_per_minute)
        self.channels = [
            PlanChannel(slot, self.roster, self.admission, policy_env, config.layout, config.fallback_scripted)
            for slot in range(len(tokens))
        ]
        self.delivered_windows = set()
        self.turn = 0
        self.last_batch_at = 0.0
        self.total_turns = max(1, config.max_steps // max(1, config.plan_interval_steps))

        self._obs_parser = ObsParser(policy_env)
        self._pass_counters = pass_counters(config.layout)
        self.heat: dict[tuple[int, int], int] = {}
        self.ticket_schedule = replay_mod.ticket_schedule(
            config.max_steps,
            interarrival=config.ticket_interarrival,
            deadline=config.ticket_deadline,
            order_queue_max=config.order_queue_max,
        )
        self.feed: list[dict[str, Any]] = []
        self.beats: list[dict[str, Any]] = []
        self.ticker: list[dict[str, Any]] = []
        self.served_by_recipe: dict[str, int] = {"salad": 0, "soup": 0, "fries": 0}
        self.orders_arrived = 0
        self.orders_expired = 0
        self.burned = {"pot": 0, "fryer": 0}
        self.state = replay_mod.TickState(step=0, cogs=[], delivered=[0] * len(self.tokens))

        self.journal = Journal.from_environment(config.as_dict(), evidence_profile=evidence_profile)
        self.writer: replay_mod.ReplayWriter | None = None
        self._artifact_sink: Callable[[bytes, bytes], None] | None = None
        self.artifact_publisher: Callable[[], Awaitable[None]] | None = None
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
        process_start: float | None = None,
        disconnect_exception_types: tuple[type[Exception], ...] = (RuntimeError,),
        request_shutdown: Callable[[], None] = lambda: None,
        evidence_profile: Literal["native_runtime", "scripted_teacher_logical_clock_v1"] = "native_runtime",
    ) -> LiveMettaGridEpisode:
        sim = Simulator().new_simulation(env, seed=config.seed)
        return cls(
            sim,
            PolicyEnvInterface.from_mg_cfg(sim.config),
            config=config,
            tokens=tokens,
            player_names=player_names,
            process_start=process_start,
            disconnect_exception_types=disconnect_exception_types,
            request_shutdown=request_shutdown,
            evidence_profile=evidence_profile,
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
            "baseline": seat.baseline if seat.kind == "scripted" else "",
            "color": seat.slot,
            "disconnected": not seat.ever_connected,
        }

    # -- connections --------------------------------------------------------
    async def connect_player(self, slot: int, websocket: PlayerWebSocket) -> str:
        if self.closing or self.roster.frozen or self.connections_by_slot[slot]:
            raise ValueError("player connection admission is closed for this seat")
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
        connection = self.connections[connection_id]
        seat = self.seats[connection.slot]

        async def ingest() -> None:
            frame = PLAYER_FRAME.validate_python(raw_message)
            await self.channels[connection.slot].receive(frame)
            if self.journal is not None and isinstance(frame, AttemptProgress):
                self.journal.append(ReceivedCall(window_id=str(frame.window_id), attempt=frame.evidence))
            if isinstance(frame, (NativeRegistration, ScriptedRegistration)) or frame.type == "register":
                seat.registered = True
                seat.kind = "prompt" if isinstance(frame, NativeRegistration) else frame.kind
                if isinstance(frame, NativeRegistration):
                    seat.prompt = frame.prompt
                if isinstance(frame, ScriptedRegistration):
                    seat.baseline = frame.baseline

        task = lifecycle.owned_task(ingest())
        await asyncio.wait({task})
        error = task.exception()
        if isinstance(error, (ValidationError, ValueError)):
            seat.action_fallbacks["malformed_frame"] += 1
        elif error is not None:
            raise error
        self._policy_action_event.set()

    # -- the loop -----------------------------------------------------------
    async def run(self) -> None:
        physics = lifecycle.owned_task(self._play())
        self.physics_task = physics
        try:
            await asyncio.wait({physics})
            physics.result()
        finally:
            joined = await lifecycle.settle({physics}, lifecycle.cleanup_deadline(), cancel=True)
            if self.journal is not None:
                self.journal.append(
                    OwnershipState(
                        joined=joined,
                        scope="engine",
                        reason="physics task terminal" if joined else "physics writer unresolved",
                    )
                )
            if not joined:
                raise lifecycle.OwnershipUnsettled("physics writer did not join before publication")
        if self.journal is not None:
            from .trajectory import finalize_owned

            self.journal.append(OwnershipState(joined=True, reason="physics writer and player receivers terminal"))
            self.journal.close_joined()
            await finalize_owned(self.journal.path, self.journal.canonical_path)
        results = self.results()
        if self._artifact_sink is not None and self.writer is not None:
            self._artifact_sink(results_mod.encode(results), self.writer.encode(results))
        if self.artifact_publisher is not None:
            await self.artifact_publisher()
        self.done = True
        await self._send_final(results)
        # Certification probes /global after players start; retain its declared grace.
        await self.wait_shutdown_grace()
        self.exited = True
        self.request_shutdown()

    async def wait_shutdown_grace(self) -> None:
        await asyncio.sleep(self.config.shutdown_grace_seconds)

    async def _play(self) -> None:
        await self._wait_for_roster()
        if not self.connected_slots():
            await self._settle("no_players")
            return
        await self._wait_for_registrations()
        self._push_event({"ev": "episode_start", "layout": self.config.layout})
        self.state = replay_mod.capture(self.sim, len(self.tokens))
        await self._send_observations()
        while not self.closing and self.sim.current_step < self.config.max_steps and not self.sim.is_done():
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
            if self.journal is not None:
                self.journal.append(
                    EngineApplication(
                        start_tick=step,
                        end_tick=int(self.sim.current_step),
                        controls=self.installed_controls,
                        actions=self.applied_control_buffers["actions"],
                        vibe_actions=self.applied_control_buffers["vibe_actions"],
                        effects=events,
                        scores=self.scores(),
                    )
                )
            self._absorb(events)
            self._record(current, events)  # 6
            self.state = current
            self._update_prompt_memory()
            await self._plan_boundary(step)  # 7
            await self._deliver_plans()  # 8
            if self.sim.current_step < self.config.max_steps and not self.sim.is_done():
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
                self.roster.register(
                    seat.slot,
                    ScriptedRegistration.model_validate({"slot": seat.slot, "baseline": self.config.fallback_scripted}),
                )
                seat.kind = "scripted"
                seat.baseline = self.config.fallback_scripted
                seat.registered = True
        self.roster.freeze()
        if self.journal is not None:
            self.journal.append(RegisteredPolicies(registrations=[self.roster.player(slot) for slot in range(4)]))
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
            waiting = lifecycle.owned_task(self._policy_action_event.wait())
            if not await lifecycle.settle({waiting}, deadline, cancel=False):
                if not await lifecycle.settle({waiting}, lifecycle.cleanup_deadline(), cancel=True):
                    raise lifecycle.OwnershipUnsettled("tick wait did not join")
                return

    def _policy_actions_ready(self, step: int) -> bool:
        return all(
            not self.connections_by_slot[slot] or self.channels[slot].submitted is not None
            for slot in range(len(self.tokens))
        )

    def _apply_actions(self, step: int) -> None:
        if self.closing:
            raise ValueError("physical engine admission is closed")
        self.installed_controls = []
        for slot, channel in enumerate(self.channels):
            cause = (
                "disconnected"
                if not self.connections_by_slot[slot]
                else "deadline"
                if channel.submitted is None
                else ""
            )
            admitted = channel.admit(cause)
            if admitted.step != step:
                raise ValueError("physical admission differs from actual engine tick")
            self.installed_controls.append(admitted)
            if admitted.cause:
                self.seats[slot].action_fallbacks.setdefault(admitted.cause, 0)
                self.seats[slot].action_fallbacks[admitted.cause] += 1
                self._push_event({"ev": "action_fallback", "slot": slot, "step": step, "cause": admitted.cause})
            self.latest_action_indices[slot] = self.action_names.index(admitted.installed_action)
            self.applied_actions[slot] = admitted.installed_action
            self.sim.agent(slot).set_action(admitted.installed_action)
        # Read the C++ input buffers after installation; submission is not application.
        self.applied_control_buffers = {
            "actions": [int(value) for value in self.sim._c_sim.actions().tolist()],
            "vibe_actions": [int(value) for value in self.sim._c_sim.vibe_actions().tolist()],
        }
        self.applied_actions = [self.sim.action_names[index] for index in self.applied_control_buffers["actions"]]
        if self.applied_actions != [control.installed_action for control in self.installed_controls]:
            raise ValueError("actual engine action buffers differ from admitted physical controls")

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
                self.ticker.append(
                    {"t": event.get("t", self.sim.current_step), "recipe": recipe, "alias": event["alias"]}
                )
                self._feed(event, f"{event['alias']} serves {recipe} - dish {dish}", "serve")
                self._beat(event, "serve", f"Dish {dish} - {event['alias']} serves {recipe}")
            elif kind == "blocked":
                slot = event["slot"]
                self.seats[slot].blocked += 1
                position = self.state.cogs[slot]["pos"]
                heat = self.seats[slot].private_heat
                heat[position] = heat.get(position, 0) + 1
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
        return any((here[0] + dr, here[1] + dc) in self._pass_counters for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)))

    def _feed(self, event: dict[str, Any], text: str, kind: str) -> None:
        self.feed.append({"t": int(self.sim.current_step), "kind": kind, "text": truncate_runes(text, FEED_RUNES)})
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

    async def _plan_boundary(self, step: int) -> None:
        if step % self.config.plan_interval_steps != 0:
            return
        if any(not entry.closed for entry in self.admission.windows.values()):
            return
        if any(entry.window.window_id not in self.delivered_windows for entry in self.admission.windows.values()):
            return
        now = time.monotonic()
        if self.last_batch_at and now - self.last_batch_at < self.config.min_plan_interval_seconds:
            return
        seats = self._prompt_seats()
        if not seats:
            return
        self.turn += 1
        self.last_batch_at = now
        windows = []
        for seat in seats:
            registration = self.roster.player(seat.slot)
            assert isinstance(registration, NativeRegistration)
            windows.append(self.private_window(seat.slot, registration.profile))
            seat.plan_pending = True
        grants = self.admission.begin(windows, self.config.plan_timeout_seconds)
        if self.journal is not None:
            for window in windows:
                self.journal.append(PrivateWindow(window=window))
            for grant in grants:
                self.journal.append(GrantedCall(grant=grant))
        await self._send_to_players(
            {
                cid: grant.model_dump(mode="json")
                for grant in grants
                for cid in self.connections_by_slot[grant.window.slot]
            }
        )

    def private_window(self, slot: int, profile: NativeProfile) -> PlanWindow:
        """Freeze one source-owned private view and the ordinary prompt renderer."""
        seat = self.seats[slot]
        view = KitchenView.model_validate(self._seat_view(seat))
        return PlanWindow(
            window_id=uuid4(),
            slot=slot,
            turn=self.turn,
            step=int(self.sim.current_step),
            alias=seat.alias,
            system=build_system_prompt(
                seat.alias,
                self.config.max_steps,
                self.config.layout,
                LAYOUT_LINES.get(self.config.layout, ""),
                self.config.plan_interval_steps,
                seat.prompt,
            ),
            user=build_user_message(view.model_dump()),
            legal_stations=view.legal_stations,
            ally_aliases=[other.alias for other in self.seats if other.slot != slot] + ["none"],
            observation=view.model_dump(mode="json"),
            profile=profile,
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

        blocked_at = max(seat.private_heat, key=lambda key: seat.private_heat[key], default=None)
        radio = [(other.alias, other.say) for other in self.seats if other.slot != seat.slot and other.say][
            :RADIO_LINES
        ]
        return {
            "turn": self.turn,
            "turns": self.total_turns,
            "tick": step,
            "ticks": self.config.max_steps,
            "layout": self.config.layout,
            "dishes": sum(self.state.delivered),
            "live": sum(board_inv.get(key, 0) for key in (QUEUE_SALAD, QUEUE_SOUP, QUEUE_FRIES)),
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

        Only registered native macro seats need prompt memory. Scripted and
        external physical controllers do not perform language inference.
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
        _, visible = self._obs_parser.parse(self.sim.agent(seat.slot).observation, fallback_position=here)
        for other in self.seats:
            if other.slot == seat.slot or other.slot >= len(self.state.cogs):
                continue
            there = self.state.cogs[other.slot]["pos"]
            if there in visible and visible[there].type == "agent":
                seat.ally_memory[other.alias] = (
                    there,
                    step,
                    replay_mod.carried_item(visible[there].properties),
                )

    def _refresh_entity_map(self, seat: Seat) -> None:
        observation = self.sim.agent(seat.slot).observation
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
        now = time.monotonic()
        for slot, entry in self.admission.windows.items():
            if entry.window.window_id in self.delivered_windows:
                continue
            if not entry.closed and now >= entry.deadline:
                entry.closed = True
                entry.rejection_reason = "timeout"
            if not entry.closed and entry.rejection_reason is not None:
                if len(entry.grants) == 2:
                    entry.closed = True
                else:
                    grant = self.admission.grant(slot, now=now)
                    if grant is not None:
                        if self.journal is not None:
                            self.journal.append(GrantedCall(grant=grant))
                        entry.rejection_reason = None
                        await self._send_to_slot(slot, grant.model_dump(mode="json"))
            if not entry.closed:
                continue
            plan = entry.parsed_plan
            installed = DirectiveInstalled(
                window_id=entry.window.window_id,
                slot=slot,
                turn=entry.window.turn,
                plan=plan,
                cause=entry.rejection_reason or "",
            )
            self.install_macro(installed, entry.selected_attempt_id, origin="llm")
            await self._send_to_slot(slot, installed.model_dump(mode="json"))

    def install_macro(
        self, installed: DirectiveInstalled, selected_attempt_id: UUID | None, *, origin: Literal["llm", "teacher"]
    ) -> None:
        """One source-owned installation path for native runtime and scripted collection."""
        slot = installed.slot
        seat = self.seats[slot]
        seat.plan_pending = False
        self.channels[slot].install(installed)
        if self.journal is not None:
            self.journal.append(InstalledMacro(directive=installed, selected_attempt_id=selected_attempt_id))
        self.delivered_windows.add(installed.window_id)
        plan = installed.plan
        if plan is not None:
            seat.say = plan.say
            seat.note = plan.note
            seat.last_order = f"station={plan.station} recipe={plan.recipe} zone={plan.zone} handoff={plan.handoff}"
            event = plan.replay_event(slot, seat.alias, installed.turn, origin)
            self._push_event(event)
            if plan.say:
                self._feed(event, f"{truncate_runes(seat.alias, ALIAS_CAP)}: {plan.say}", "say")
            self._beat({}, "plan", f"{seat.alias} takes {plan.station}")
            seat.plan_delivered_step = int(self.sim.current_step)
        else:
            seat.fallbacks += 1
            self._push_event({"ev": "fallback", "slot": slot, "alias": seat.alias, "cause": installed.cause})

    # -- messages -----------------------------------------------------------
    async def _send_observations(self) -> None:
        windows = {
            slot: self.channels[slot]
            .observe(self.private_observation(slot), int(self.sim.current_step))
            .model_dump(mode="json")
            for slot in range(len(self.tokens))
        }
        if self.journal is not None:
            for window in windows.values():
                self.journal.append(PrivateTick(window=PhysicalObservation.model_validate(window)))
        await self._send_to_players({cid: windows[connection.slot] for cid, connection in self.connections.items()})

    async def _send_to_slot(self, slot: int, message: dict[str, Any]) -> None:
        await self._send_to_players({connection_id: message for connection_id in self.connections_by_slot[slot]})

    async def _send_to_players(self, messages: dict[str, dict[str, Any]]) -> None:
        connections = tuple(
            (connection_id, self.connections[connection_id])
            for connection_id in messages
            if connection_id in self.connections
        )
        if not connections:
            return
        deadline = lifecycle.cleanup_deadline()
        sends = [
            (cid, lifecycle.owned_task(connection.websocket.send_json(messages[cid])))
            for cid, connection in connections
        ]
        if not await lifecycle.settle({task for _, task in sends}, deadline, cancel=False):
            joined = await lifecycle.settle({task for _, task in sends}, deadline, cancel=True)
            if not joined:
                raise lifecycle.OwnershipUnsettled("engine socket writer did not join")
        for connection_id, task in sends:
            if task.cancelled():
                self.disconnect_player(connection_id)
            else:
                error = task.exception()
                if error is None:
                    continue
                if isinstance(error, lifecycle.OwnershipUnsettled):
                    raise error
                if isinstance(error, self.disconnect_exception_types):
                    self.disconnect_player(connection_id)
                else:
                    raise error

    def player_config_message(self, slot: int, connection_id: str) -> dict[str, Any]:
        return PlayerConfiguration.model_validate(
            {"slot": slot, "alias": self.seats[slot].alias, "layout": self.config.layout, "policy_env": self.policy_env}
        ).model_dump(mode="json")

    def private_observation(self, slot: int) -> list[list[int]]:
        return self.sim._c_sim.observations()[slot].tolist()

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
        result = results_mod.build_results(
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
            llm_requests=sum(len(entry.grants) for entry in self.admission.history.values()),
        )
        result["action_fallbacks"] = [dict(seat.action_fallbacks) for seat in self.seats]
        return result

    async def stop_players(self, deadline: float) -> None:
        """One absolute budget joins actual receivers before private or public sealing."""
        if self.admission.sealed:
            return
        self.closing = True
        self.admission.stop()
        stops = {slot: channel.stop() for slot, channel in enumerate(self.channels)}
        players_joined = False
        try:
            async with lifecycle.own_until(deadline):
                await self._send_to_players(
                    {
                        cid: stops[connection.slot].model_dump(mode="json")
                        for cid, connection in self.connections.items()
                    }
                )
                required = {seat.slot for seat in self.seats if seat.ever_connected}
                while not all(self.channels[slot].acknowledged for slot in required):
                    self._policy_action_event.clear()
                    if all(self.channels[slot].acknowledged for slot in required):
                        break
                    waiting = lifecycle.owned_task(self._policy_action_event.wait())
                    if not await lifecycle.settle({waiting}, deadline, cancel=False):
                        await lifecycle.settle({waiting}, deadline, cancel=True)
                        raise lifecycle.OwnershipUnsettled(
                            "engine stop lacks every registered player's joined acknowledgement"
                        )
                await self._send_to_players(
                    {
                        cid: self.channels[connection.slot].evidence_received().model_dump(mode="json")
                        for cid, connection in self.connections.items()
                    }
                )
                if not await lifecycle.settle(self.player_receivers, deadline, cancel=False):
                    raise lifecycle.OwnershipUnsettled("game socket receivers did not join before sealing")
            self.admission.seal()
            players_joined = True
        finally:
            if self.journal is not None:
                self.journal.append(
                    OwnershipState(
                        joined=players_joined,
                        scope="players",
                        reason="every registered player receiver joined"
                        if players_joined
                        else "player ownership unresolved",
                    )
                )

    async def close_owner(self, deadline: float) -> bool:
        """Close admission before asking the owned engine writer to terminate."""
        self.closing = True
        self.admission.stop()
        for channel in self.channels:
            channel.stop()
        writers = {
            task
            for task in (self.play_task, self.physics_task)
            if task is not None and task is not asyncio.current_task()
        }
        if not await lifecycle.settle(writers, deadline, cancel=True):
            if self.journal is not None:
                self.journal.append(OwnershipState(joined=False, scope="engine", reason="engine writer unresolved"))
            return False
        for task in writers:
            if not task.cancelled():
                error = task.exception()
                if error is not None:
                    raise error
        await self.stop_players(deadline)
        if self.journal is not None and not self.journal.closed:
            self.journal.append(
                OwnershipState(
                    joined=True, scope="episode", reason="engine task and every acquired player receiver joined"
                )
            )
            self.journal.close_joined()
        return True

    async def _settle(self, reason: str) -> None:
        self.reason = reason
        self.closing = True
        if reason == "deadline":
            self._push_event({"ev": "deadline", "step": int(self.sim.current_step)})
        self._push_event({"ev": "episode_end", "reason": reason})
        await self.stop_players(lifecycle.cleanup_deadline())
        results = self.results()
        if self.journal is not None:
            self.journal.append(EngineOutcome(reason=reason, results=results))
        if self.writer is not None:
            self.writer.seats = [self.seat_block(seat) for seat in self.seats]
            self.writer.heat = self.heat

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
            "stations": replay_mod.station_summary(self.state, self.ticket_schedule),
            "cogs": cogs,
            "feed": self.feed[-FEED_LINES:],
        }


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

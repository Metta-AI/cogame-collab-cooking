"""The replay document: capture, event derivation, and the writer.

One UTF-8 JSON document written to ``COGAME_SAVE_REPLAY_URI``. It is
self-sufficient: `docker_smoke.sh` parses it, the wasm module parses it in the
browser, and **nothing else is ever contacted** -- no server, no config
lookup, no name service. Everything the viewer needs is here: aliases and real
names, the resolved config, the seed, the kitchen grid and station positions,
every tick's cog and station state, every event, the heat map and the results.

Coordinates are ``x = column, y = row`` throughout, which is what the viewer
draws in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from collab_cooking.game.game import (
    CARRIED_ITEM_PRIORITY,
    CHOP_MEAT_PROGRESS,
    CHOP_VEG_PROGRESS,
    CHOPPED_MEAT,
    CHOPPED_VEG,
    CLEAN_PLATE,
    DIRTY_PLATE,
    DISH_RESOURCE_BY_RECIPE,
    FRYER_FRIES_BURNED,
    FRYER_FRIES_COOKING,
    FRYER_FRIES_READY,
    FRYER_TIMER,
    ORDER_QUEUE_MAX,
    POT_SOUP_BURNED,
    POT_SOUP_COOKING,
    POT_SOUP_READY,
    POT_TIMER,
    QUEUE_FRIES,
    QUEUE_SALAD,
    QUEUE_SOUP,
    RECIPE_BY_DISH_RESOURCE,
    TICKET_DEADLINE,
    TICKET_INTERARRIVAL,
    WASH_PROGRESS,
    TicketSchedule,
    build_ticket_specs,
)
from collab_cooking.kitchens.layouts import dimensions, grid, stations

REPLAY_FORMAT = "collab-cooking/1"
REPLAY_PROTOCOL = "collab-cooking.replay.v1"
REPLAY_VERSION = "0.1.0"
TILE_PX = 24

# The complete event vocabulary the replay may carry, and the only names the
# viewer must know. Per-tick movement is NOT an event -- it is in `c`.
EVENT_NAMES: tuple[str, ...] = (
    "episode_start",
    "order_arrive",
    "order_expire",
    "pickup",
    "deposit",
    "chop_start",
    "chop_done",
    "pot_load",
    "pot_start",
    "pot_ready",
    "pot_burn",
    "pot_clear",
    "fry_start",
    "fry_ready",
    "fry_burn",
    "fry_clear",
    "plate_up",
    "serve",
    "wash_start",
    "wash_done",
    "blocked",
    "plan",
    "fallback",
    "deadline",
    "episode_end",
)

# The order step 5 of the resolution order derives events in. Nothing in this
# list is order-independent, so the list IS the specification.
DIFF_ORDER: tuple[str, ...] = (
    "order_arrive",
    "order_expire",
    "pickup",
    "deposit",
    "chop_start",
    "chop_done",
    "pot_load",
    "pot_start",
    "pot_ready",
    "pot_burn",
    "pot_clear",
    "fry_start",
    "fry_ready",
    "fry_burn",
    "fry_clear",
    "plate_up",
    "serve",
    "wash_start",
    "wash_done",
    "blocked",
)

STATION_KINDS: tuple[str, ...] = (
    "veg_station",
    "meat_station",
    "plate_station",
    "chopping_station",
    "cooking_station",
    "fryer_station",
    "serving_station",
    "wash_station",
    "order_board",
)


def carried_item(inventory: dict[str, int]) -> str:
    for resource in CARRIED_ITEM_PRIORITY:
        if inventory.get(resource, 0) > 0:
            return resource
    return ""


def pot_state(inv: dict[str, int]) -> str:
    if inv.get(POT_SOUP_BURNED, 0) > 0:
        return "burned"
    if inv.get(POT_SOUP_READY, 0) > 0:
        return "ready"
    if inv.get(POT_SOUP_COOKING, 0) > 0:
        return "cooking"
    if inv.get(CHOPPED_VEG, 0) > 0 or inv.get(CHOPPED_MEAT, 0) > 0:
        return "loaded"
    return "idle"


def fryer_state(inv: dict[str, int]) -> str:
    if inv.get(FRYER_FRIES_BURNED, 0) > 0:
        return "burned"
    if inv.get(FRYER_FRIES_READY, 0) > 0:
        return "ready"
    if inv.get(FRYER_FRIES_COOKING, 0) > 0:
        return "cooking"
    return "idle"


def ticket_schedule(
    max_steps: int,
    *,
    interarrival: int = TICKET_INTERARRIVAL,
    deadline: int = TICKET_DEADLINE,
    order_queue_max: int = ORDER_QUEUE_MAX,
) -> TicketSchedule:
    """The episode's ticket schedule, keyed by the slot each ticket occupies.

    Read from the schedule the env itself lays down at config time
    (`build_ticket_specs`, the same call `make_env` makes), so the ticket index
    the viewer reads and the tick it counts down to are the ticket the engine
    put in that slot and the tick the engine expires it on.
    """
    return TicketSchedule(
        build_ticket_specs(
            max_steps,
            interarrival=interarrival,
            deadline=deadline,
            order_queue_max=order_queue_max,
        )
    )


def _live_tickets(board: dict[str, int]) -> list[str]:
    return sorted(name for name, value in board.items() if name.startswith("ticket_") and value > 0)


@dataclass
class TickState:
    """Everything step 4 reads, in the shape step 5 diffs."""

    step: int = 0
    cogs: list[dict[str, Any]] = field(default_factory=list)
    stations: dict[str, dict[str, int]] = field(default_factory=dict)
    station_pos: dict[str, tuple[int, int]] = field(default_factory=dict)
    counters: dict[tuple[int, int], str] = field(default_factory=dict)
    delivered: list[int] = field(default_factory=list)

    @property
    def board(self) -> dict[str, int]:
        return self.stations.get("order_board", {})


def _named_inventory(inventory: Any, resource_names: list[str]) -> dict[str, int]:
    """mettagrid keys inventories by resource ID; the replay speaks names."""
    named: dict[str, int] = {}
    for key, value in (inventory or {}).items():
        if not value:
            continue
        if isinstance(key, int):
            if 0 <= key < len(resource_names):
                named[resource_names[key]] = int(value)
        else:
            named[str(key)] = int(value)
    return named


def capture(sim: Any, num_agents: int) -> TickState:
    """Step 4 of the resolution order: read the settled state."""
    state = TickState(step=int(sim.current_step))
    resource_names = list(sim.resource_names)
    positions: dict[int, tuple[int, int]] = {}
    ordered_agents: list[tuple[int, tuple[int, int]]] = []
    # ONE scan of the grid: stations, counters and cog positions all come out
    # of it, because `grid_objects()` rebuilds a dict per object and calling it
    # once per cog as well is the difference between 1 ms and 5 ms a tick.
    for obj in sim.grid_objects().values():
        kind = obj.get("type_name", "")
        pos = (int(obj.get("r", 0)), int(obj.get("c", 0)))
        if kind in STATION_KINDS:
            state.stations[kind] = _named_inventory(obj.get("inventory"), resource_names)
            state.station_pos[kind] = pos
        elif kind == "wall":
            item = carried_item(_named_inventory(obj.get("inventory"), resource_names))
            if item:
                state.counters[pos] = item
        elif kind == "agent":
            agent_id = obj.get("agent_id")
            ordered_agents.append((int(obj.get("id", 0)), pos))
            if isinstance(agent_id, int):
                positions[agent_id] = pos
    if not positions:
        ordered_agents.sort()
        positions = {slot: pos for slot, (_id, pos) in enumerate(ordered_agents)}
    for slot in range(num_agents):
        agent = sim.agent(slot)
        inv = _named_inventory(agent.inventory, resource_names)
        state.cogs.append(
            {
                "pos": positions.get(slot, (0, 0)),
                "carrying": carried_item(inv),
                "success": bool(agent.last_action_success),
            }
        )
    state.delivered = [int(round(v)) for v in sim.episode_rewards.tolist()]
    return state


def derive_events(
    previous: TickState,
    current: TickState,
    actions: list[str],
    aliases: list[str],
    heat: dict[tuple[int, int], int],
) -> list[dict[str, Any]]:
    """Step 5: diff this tick's state against the previous tick's.

    The order is `DIFF_ORDER` and is fixed; ties everywhere resolve by
    ascending slot.
    """
    events: list[dict[str, Any]] = []
    prev_board = previous.board
    board = current.board
    prev_live = set(_live_tickets(prev_board))
    live = set(_live_tickets(board))

    def board_pos() -> dict[str, int]:
        r, c = current.station_pos.get("order_board", (0, 0))
        return {"x": c, "y": r}

    for name in sorted(live - prev_live):
        recipe = name.rsplit("_", 1)[-1]
        events.append({"ev": "order_arrive", "ticket": name, "recipe": recipe, **board_pos()})
    # The tickets that left the board this tick, split once into the ones a
    # serve took and the ones that expired. The state diff carries no ticket
    # identity, so the split is by count -- but it is ONE split, and the serve
    # events below draw their recipes from the same list in the same order, so
    # a tick with two serves no longer reports the first departed ticket's
    # recipe twice (r2 review R2-O8).
    departed = sorted(prev_live - live)
    served_now = max(0, sum(current.delivered) - sum(previous.delivered))
    served_tickets, expired_tickets = departed[:served_now], departed[served_now:]
    for name in expired_tickets:
        recipe = name.rsplit("_", 1)[-1]
        events.append({"ev": "order_expire", "ticket": name, "recipe": recipe, **board_pos()})

    for slot, (before, after) in enumerate(zip(previous.cogs, current.cogs, strict=False)):
        alias = aliases[slot] if slot < len(aliases) else f"Cog-{slot}"
        y, x = after["pos"]
        was, now = before["carrying"], after["carrying"]
        if now and not was:
            events.append({"ev": "pickup", "slot": slot, "alias": alias, "item": now, "x": x, "y": y})
        elif was and not now:
            events.append({"ev": "deposit", "slot": slot, "alias": alias, "item": was, "x": x, "y": y})
        elif was and now and was != now:
            events.append({"ev": "pickup", "slot": slot, "alias": alias, "item": now, "x": x, "y": y})
        if now in RECIPE_BY_DISH_RESOURCE and was == CLEAN_PLATE:
            events.append(
                {
                    "ev": "plate_up",
                    "slot": slot,
                    "alias": alias,
                    "recipe": RECIPE_BY_DISH_RESOURCE[now],
                    "x": x,
                    "y": y,
                }
            )

    events.extend(_station_events(previous, current))

    # `serve` is attributed to the seat whose delivered count went up.
    serving = current.station_pos.get("serving_station", (0, 0))
    dish_total = sum(previous.delivered)
    recipes = [name.rsplit("_", 1)[-1] for name in served_tickets]
    for slot, (was, now) in enumerate(zip(previous.delivered, current.delivered, strict=False)):
        alias = aliases[slot] if slot < len(aliases) else f"Cog-{slot}"
        for extra in range(now - was):
            del extra
            dish_total += 1
            events.append(
                {
                    "ev": "serve",
                    "slot": slot,
                    "alias": alias,
                    "recipe": recipes.pop(0) if recipes else _queue_recipe(previous, current),
                    "dish": dish_total,
                    "x": serving[1],
                    "y": serving[0],
                }
            )

    for slot, (action, cog) in enumerate(zip(actions, current.cogs, strict=False)):
        if not action.startswith("move_") or cog["success"]:
            continue
        alias = aliases[slot] if slot < len(aliases) else f"Cog-{slot}"
        y, x = cog["pos"]
        target = _move_target(cog["pos"], action)
        occupied = any(other["pos"] == target for i, other in enumerate(current.cogs) if i != slot)
        # Keyed by the tile the event carries -- the cog's own -- so the
        # replay's end-of-episode `heat` is exactly what the viewer
        # accumulates live from the `blocked` events as the playhead moves.
        # Keying it by the target tile made the two name different tiles.
        heat[(x, y)] = heat.get((x, y), 0) + 1
        events.append(
            {
                "ev": "blocked",
                "slot": slot,
                "alias": alias,
                "x": x,
                "y": y,
                "by": "cog" if occupied else "wall",
            }
        )
    # The list a tick carries IS `DIFF_ORDER`. Emission is convenient rather
    # than ordered -- `plate_up` comes out inside the per-cog loop and the
    # station events before `serve` -- so put it in the declared order here.
    # The sort is stable, so ties still resolve by ascending slot.
    rank = {name: index for index, name in enumerate(DIFF_ORDER)}
    events.sort(key=lambda event: rank.get(event["ev"], len(DIFF_ORDER)))
    return events


def _queue_recipe(previous: TickState, current: TickState) -> str:
    """The recipe a serve took when no ticket left the board to name it."""
    for recipe, resource in (("salad", QUEUE_SALAD), ("soup", QUEUE_SOUP), ("fries", QUEUE_FRIES)):
        if current.board.get(resource, 0) < previous.board.get(resource, 0):
            return recipe
    return "salad"


def _move_target(pos: tuple[int, int], action: str) -> tuple[int, int]:
    r, c = pos
    return {
        "move_north": (r - 1, c),
        "move_south": (r + 1, c),
        "move_west": (r, c - 1),
        "move_east": (r, c + 1),
    }.get(action, pos)


def _station_events(previous: TickState, current: TickState) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    def pos_of(kind: str) -> dict[str, int]:
        r, c = current.station_pos.get(kind, (0, 0))
        return {"x": c, "y": r}

    chop_before = previous.stations.get("chopping_station", {})
    chop_after = current.stations.get("chopping_station", {})
    for progress, what in ((CHOP_VEG_PROGRESS, "veg"), (CHOP_MEAT_PROGRESS, "meat")):
        before, after = chop_before.get(progress, 0), chop_after.get(progress, 0)
        if after > before and before == 0:
            events.append({"ev": "chop_start", "what": what, **pos_of("chopping_station")})
        elif before > 0 and after == 0:
            events.append({"ev": "chop_done", "what": what, **pos_of("chopping_station")})

    pot_before = pot_state(previous.stations.get("cooking_station", {}))
    pot_after = pot_state(current.stations.get("cooking_station", {}))
    if pot_before != pot_after:
        name = {
            "loaded": "pot_load",
            "cooking": "pot_start",
            "ready": "pot_ready",
            "burned": "pot_burn",
            "idle": "pot_clear",
        }[pot_after]
        events.append({"ev": name, "state": pot_after, **pos_of("cooking_station")})

    fry_before = fryer_state(previous.stations.get("fryer_station", {}))
    fry_after = fryer_state(current.stations.get("fryer_station", {}))
    if fry_before != fry_after:
        name = {
            "cooking": "fry_start",
            "ready": "fry_ready",
            "burned": "fry_burn",
            "idle": "fry_clear",
            "loaded": "fry_start",
        }[fry_after]
        events.append({"ev": name, "state": fry_after, **pos_of("fryer_station")})

    wash_before = previous.stations.get("wash_station", {}).get(WASH_PROGRESS, 0)
    wash_after = current.stations.get("wash_station", {}).get(WASH_PROGRESS, 0)
    if wash_after > wash_before and wash_before == 0:
        events.append({"ev": "wash_start", **pos_of("wash_station")})
    elif wash_before > 0 and wash_after == 0:
        events.append({"ev": "wash_done", **pos_of("wash_station")})
    return events


def _ticket_entry(name: str, step: int, schedule: TicketSchedule | None) -> dict[str, Any]:
    """One live ticket, reported by its GLOBAL index and its own expiry tick.

    The resource names a recycled slot, so the ticket that owns it is looked up
    in the schedule; `i` stays the ticket's index in the episode's ticket
    sequence, which is what the replay schema and the viewer's countdown read.
    """
    slot = int(name.split("_")[1])
    spec = schedule.occupant(slot, step) if schedule is not None else None
    if spec is None:
        return {"i": slot, "recipe": name.rsplit("_", 1)[-1], "expires": -1}
    return {"i": spec.index, "recipe": spec.recipe, "expires": spec.expiry}


def station_summary(state: TickState, schedule: TicketSchedule | None = None) -> dict[str, Any]:
    """The compact `st` block the viewer reads."""
    chop = state.stations.get("chopping_station", {})
    pot = state.stations.get("cooking_station", {})
    fryer = state.stations.get("fryer_station", {})
    sink = state.stations.get("wash_station", {})
    board = state.board
    return {
        "chop": {"veg": chop.get(CHOP_VEG_PROGRESS, 0), "meat": chop.get(CHOP_MEAT_PROGRESS, 0)},
        "pot": {"state": pot_state(pot), "timer": pot.get(POT_TIMER, 0)},
        "fryer": {"state": fryer_state(fryer), "timer": fryer.get(FRYER_TIMER, 0)},
        "sink": {"wash": sink.get(WASH_PROGRESS, 0)},
        "board": {
            "salad": board.get(QUEUE_SALAD, 0),
            "soup": board.get(QUEUE_SOUP, 0),
            "fries": board.get(QUEUE_FRIES, 0),
            # `expires` is the absolute tick this ticket dies on: the clock
            # readout counts an order EXPIRING from it, so a ticket without
            # one can never make "3 ORDERS LIVE - 1 EXPIRING" fire.
            "tickets": [_ticket_entry(name, state.step, schedule) for name in _live_tickets(board)],
        },
        "counters": sorted([c, r, item] for (r, c), item in state.counters.items()),
    }


class ReplayWriter:
    """Accumulates the tick records and emits the document."""

    def __init__(
        self,
        *,
        layout: str,
        seed: int,
        config: dict[str, Any],
        seats: list[dict[str, Any]],
        generated_at: str,
    ) -> None:
        self.layout = layout
        self.seed = seed
        self.config = config
        self.seats = seats
        self.generated_at = generated_at
        self.ticket_schedule = ticket_schedule(
            int(config.get("max_steps", 0) or 0),
            interarrival=int(config.get("ticket_interarrival", TICKET_INTERARRIVAL) or TICKET_INTERARRIVAL),
            deadline=int(config.get("ticket_deadline", TICKET_DEADLINE) or TICKET_DEADLINE),
            order_queue_max=int(config.get("order_queue_max", ORDER_QUEUE_MAX) or ORDER_QUEUE_MAX),
        )
        self.ticks: list[dict[str, Any]] = []
        self.heat: dict[tuple[int, int], int] = {}
        self._last_stations: dict[str, Any] | None = None

    def append(
        self,
        state: TickState,
        actions: list[str],
        flags: list[int],
        events: list[dict[str, Any]],
    ) -> None:
        record: dict[str, Any] = {
            "t": state.step,
            "c": [
                [cog["pos"][1], cog["pos"][0], cog["carrying"], actions[i] if i < len(actions) else "noop", flags[i]]
                for i, cog in enumerate(state.cogs)
            ],
            "sc": list(state.delivered),
        }
        summary = station_summary(state, self.ticket_schedule)
        if summary != self._last_stations:
            record["st"] = summary
            self._last_stations = summary
        if events:
            record["ev"] = events
        self.ticks.append(record)

    def kitchen_block(self) -> dict[str, Any]:
        width, height = dimensions(self.layout)
        return {
            "w": width,
            "h": height,
            "tile": TILE_PX,
            "rows": grid(self.layout),
            "stations": [
                {"kind": kind, "x": pos[1], "y": pos[0]}
                for kind, pos in sorted(stations(self.layout).items())
            ],
        }

    def document(self, results: dict[str, Any]) -> dict[str, Any]:
        return {
            "format": REPLAY_FORMAT,
            "protocol": REPLAY_PROTOCOL,
            "version": REPLAY_VERSION,
            "coworld": "collab_cooking",
            "layout": self.layout,
            "generated_at": self.generated_at,
            "seed": self.seed,
            "config": self.config,
            "kitchen": self.kitchen_block(),
            "seats": self.seats,
            "ticks": self.ticks,
            "heat": sorted([x, y, n] for (x, y), n in self.heat.items()),
            "results": results,
        }

    def encode(self, results: dict[str, Any]) -> bytes:
        """UTF-8 exactly once, `ensure_ascii=False`."""
        return json.dumps(self.document(results), ensure_ascii=False).encode("utf-8")

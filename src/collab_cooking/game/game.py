"""Game configuration for the Collaborative Cooking kitchen.

Ported from `coworld-overcogged`'s `src/overcogged/game/game.py`. Four
changes, and only four:

* the map builder is swapped from the procedural `MapGenConfig` /
  `CompoundConfig` hub to the eight hand-authored ASCII kitchens
  (`collab_cooking.kitchens.layouts`), so `hub_*`, `station_offsets`,
  `station_order` and their validators are gone with it;
* `_agent_config()`'s rewards are reduced to exactly one term,
  `orders_served` -- the rankable quantity has to BE dishes, not a shaped
  proxy of dishes, so `sim.episode_rewards[i]` is exactly the integer count of
  dishes seat i carried to the pass;
* the `full` mechanics set is forced on (the starter turned the recipes on
  through a CoGame variant graph that the pinned `mettagrid` no longer ships);
* tickets are encoded as a **bounded pool of recycled slot resources**
  (`ticket_slot_count`) instead of one resource per prospective ticket. The
  schedule is untouched -- 50 tickets in a 900-tick episode, 18 ticks apart,
  alive for 50 -- but the resource list, and therefore mettagrid's one-byte
  feature-id space, no longer grows with `max_steps`.

Everything else -- station handlers, ticket specs, events, render config -- is
the starter's, unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from mettagrid.config.action_config import (
    ActionsConfig,
    ChangeVibeActionConfig,
    MoveActionConfig,
    NoopActionConfig,
)
from mettagrid.config.event_config import EventConfig, periodic
from mettagrid.config.filter import GameValueFilter, HandlerTarget, actorHasAnyOf, isNot, targetHasAnyOf
from mettagrid.config.game_value import QueryInventoryValue, SumGameValue, stat
from mettagrid.config.handler_config import (
    Handler,
    actorHas,
    deposit,
    firstMatch,
    queryDelta,
    targetHas,
    updateActor,
    updateTarget,
    withdraw,
)
from mettagrid.config.mettagrid_config import (
    AgentConfig,
    GameConfig,
    GridObjectConfig,
    InventoryConfig,
    MettaGridConfig,
    ResourceLimitsConfig,
    TalkConfig,
    WallConfig,
)
from mettagrid.config.mutation.stats_mutation import logActorAgentStat, logStatToGame
from mettagrid.config.obs_config import GlobalObsConfig, ObsConfig
from mettagrid.config.query import Query, query
from mettagrid.config.render_config import RenderAsset, RenderConfig, RenderHudConfig, RenderStatusBarConfig
from mettagrid.config.reward_config import AgentReward, reward
from mettagrid.config.tag import typeTag

from collab_cooking.kitchens.layouts import kitchen

RecipeName = Literal["salad", "soup", "fries"]

VEG = "veg"
MEAT = "meat"
CHOPPED_VEG = "chopped_veg"
CHOPPED_MEAT = "chopped_meat"
CHOP_VEG_PROGRESS = "chop_veg_progress"
CHOP_MEAT_PROGRESS = "chop_meat_progress"

CLEAN_PLATE = "clean_plate"
DIRTY_PLATE = "dirty_plate"
WASH_PROGRESS = "wash_progress"

DISH_SALAD = "dish_salad"
DISH_SOUP = "dish_soup"
DISH_FRIES = "dish_fries"

QUEUE_SALAD = "queue_salad"
QUEUE_SOUP = "queue_soup"
QUEUE_FRIES = "queue_fries"

POT_SOUP_COOKING = "pot_soup_cooking"
POT_SOUP_READY = "pot_soup_ready"
POT_SOUP_BURNED = "pot_soup_burned"
POT_TIMER = "pot_timer"
POT_READY_AGE = "pot_ready_age"

FRYER_FRIES_COOKING = "fryer_fries_cooking"
FRYER_FRIES_READY = "fryer_fries_ready"
FRYER_FRIES_BURNED = "fryer_fries_burned"
FRYER_TIMER = "fryer_timer"
FRYER_READY_AGE = "fryer_ready_age"

BASE_AGENT_RESOURCES = [
    VEG,
    MEAT,
    CHOPPED_VEG,
    CHOPPED_MEAT,
    CLEAN_PLATE,
    DIRTY_PLATE,
    DISH_SALAD,
    DISH_SOUP,
    DISH_FRIES,
]
PREP_PROGRESS_RESOURCES = [CHOP_VEG_PROGRESS, CHOP_MEAT_PROGRESS, WASH_PROGRESS]
QUEUE_COUNTER_RESOURCES = [QUEUE_SALAD, QUEUE_SOUP, QUEUE_FRIES]
POT_RESOURCES = [POT_SOUP_COOKING, POT_SOUP_READY, POT_SOUP_BURNED, POT_TIMER, POT_READY_AGE]
FRYER_RESOURCES = [FRYER_FRIES_COOKING, FRYER_FRIES_READY, FRYER_FRIES_BURNED, FRYER_TIMER, FRYER_READY_AGE]

SOUP_COOK_TICKS = 10
SOUP_BURN_TICKS = 14
FRIES_COOK_TICKS = 8
FRIES_BURN_TICKS = 11

ORDER_QUEUE_MAX = 8
CHOP_TICKS = 3
WASH_TICKS = 3

TICKET_FIRST_ARRIVAL = 0
TICKET_INTERARRIVAL = 18
TICKET_DEADLINE = 50

# The ticket-slot pool is what keeps the resource list -- and therefore the
# feature-id space -- independent of `max_steps`. mettagrid packs a feature id
# into one byte (`token_value_base = 256`) and every resource costs four ids
# (`inv:`, `inv::p1`, `protocol_input:`, `protocol_output:`), so a pool this
# small leaves the whole board well under 200 ids at any episode length.
TICKET_SLOT_CAP = 20

ORDER_BOARD_QUERY: Query = query(typeTag("order_board"))
COOKING_STATION_QUERY: Query = query(typeTag("cooking_station"))
FRYER_STATION_QUERY: Query = query(typeTag("fryer_station"))

STATIONS = [
    "veg_station",
    "meat_station",
    "plate_station",
    "chopping_station",
    "cooking_station",
    "fryer_station",
    "serving_station",
    "wash_station",
    "order_board",
]
RECIPE_CYCLE: tuple[RecipeName, ...] = ("soup", "salad", "soup", "fries", "salad")
QUEUE_RESOURCE_BY_RECIPE: dict[RecipeName, str] = {
    "salad": QUEUE_SALAD,
    "soup": QUEUE_SOUP,
    "fries": QUEUE_FRIES,
}
DISH_RESOURCE_BY_RECIPE: dict[RecipeName, str] = {
    "salad": DISH_SALAD,
    "soup": DISH_SOUP,
    "fries": DISH_FRIES,
}
RECIPE_BY_DISH_RESOURCE: dict[str, RecipeName] = {v: k for k, v in DISH_RESOURCE_BY_RECIPE.items()}
AGENT_HUD_SPECS: tuple[tuple[str, str], ...] = (
    (DISH_SALAD, "SD"),
    (DISH_SOUP, "SP"),
    (DISH_FRIES, "FR"),
    (CLEAN_PLATE, "PL"),
    (DIRTY_PLATE, "DP"),
)
AGENT_STATUS_SPECS: tuple[tuple[str, str], ...] = (
    (CHOPPED_VEG, "CV"),
    (CHOPPED_MEAT, "CM"),
    (DISH_SALAD, "SD"),
    (DISH_SOUP, "SP"),
    (DISH_FRIES, "FR"),
    (CLEAN_PLATE, "PL"),
    (DIRTY_PLATE, "DP"),
)
CARRIED_ITEM_PRIORITY: tuple[str, ...] = (
    DISH_SALAD,
    DISH_SOUP,
    DISH_FRIES,
    CLEAN_PLATE,
    DIRTY_PLATE,
    CHOPPED_VEG,
    CHOPPED_MEAT,
    VEG,
    MEAT,
)


@dataclass(frozen=True, slots=True)
class TicketSpec:
    index: int
    slot: int
    recipe: RecipeName
    arrival: int
    expiry: int
    resource: str

    @property
    def queue_resource(self) -> str:
        return queue_resource_for_recipe(self.recipe)


@dataclass(slots=True)
class KitchenSettings:
    """Every tunable the env build reads. All eight kitchens share one economy."""

    layout: str = "open-kitchen"
    num_agents: int = 4
    max_steps: int = 900
    ticket_first_arrival: int = TICKET_FIRST_ARRIVAL
    ticket_interarrival: int = TICKET_INTERARRIVAL
    ticket_deadline: int = TICKET_DEADLINE
    chop_ticks: int = CHOP_TICKS
    wash_ticks: int = WASH_TICKS
    soup_cook_ticks: int = SOUP_COOK_TICKS
    soup_burn_ticks: int = SOUP_BURN_TICKS
    fries_cook_ticks: int = FRIES_COOK_TICKS
    fries_burn_ticks: int = FRIES_BURN_TICKS
    order_queue_max: int = ORDER_QUEUE_MAX


def queue_resource_for_recipe(recipe: RecipeName) -> str:
    return QUEUE_RESOURCE_BY_RECIPE[recipe]


def dish_resource_for_recipe(recipe: RecipeName) -> str:
    return DISH_RESOURCE_BY_RECIPE[recipe]


def _ticket_resource_name(slot: int, recipe: RecipeName) -> str:
    return f"ticket_{slot:03d}_{recipe}"


def ticket_slot_count(
    *,
    interarrival: int = TICKET_INTERARRIVAL,
    deadline: int = TICKET_DEADLINE,
    order_queue_max: int = ORDER_QUEUE_MAX,
) -> int:
    """How many ticket-slot resources the order board needs.

    A slot is the identity of ONE live ticket and is reused by the next ticket
    scheduled into it once that one has been served or has expired, so the
    resource count is bounded by the board rather than by `max_steps`. Three
    constraints fix the size:

    * a multiple of `len(RECIPE_CYCLE)`, so slot `s` always carries recipe
      `RECIPE_CYCLE[s % len(RECIPE_CYCLE)]` and the resource name still names
      the recipe it is a ticket for;
    * strictly more than the number of tickets whose lifetimes can overlap
      (`deadline // interarrival + 1`), so the slot a ticket is scheduled into
      is always free when it arrives;
    * more than `order_queue_max`, the most tickets that can be live at once.

    Capped at `TICKET_SLOT_CAP` (a multiple of the recipe cycle) because the
    feature-id space is one byte wide. A capped pool cannot corrupt the board:
    an arrival whose slot is still occupied is skipped exactly like an arrival
    into a full queue. It would only tighten the queue for schedules far denser
    than any shipped variant's (18 ticks apart, 50 ticks alive).
    """
    if interarrival <= 0:
        raise ValueError("interarrival must be positive")
    cycle = len(RECIPE_CYCLE)
    overlapping = deadline // interarrival + 1
    needed = max(order_queue_max, overlapping) + 1
    slots = -(-needed // cycle) * cycle
    return min(slots, TICKET_SLOT_CAP)


def ticket_slot_resources(slots: int) -> list[str]:
    """The whole pool, in slot order. Slot `s` is always the same recipe."""
    return [_ticket_resource_name(slot, RECIPE_CYCLE[slot % len(RECIPE_CYCLE)]) for slot in range(slots)]


def build_ticket_specs(
    max_steps: int,
    *,
    first_arrival: int = TICKET_FIRST_ARRIVAL,
    interarrival: int = TICKET_INTERARRIVAL,
    deadline: int = TICKET_DEADLINE,
    order_queue_max: int = ORDER_QUEUE_MAX,
) -> list[TicketSpec]:
    if interarrival <= 0:
        raise ValueError("interarrival must be positive")
    if deadline <= 0:
        raise ValueError("deadline must be positive")

    slots = ticket_slot_count(
        interarrival=interarrival, deadline=deadline, order_queue_max=order_queue_max
    )
    specs: list[TicketSpec] = []
    arrival = first_arrival
    idx = 0
    while arrival < max_steps:
        recipe = RECIPE_CYCLE[idx % len(RECIPE_CYCLE)]
        slot = idx % slots
        specs.append(
            TicketSpec(
                index=idx,
                slot=slot,
                recipe=recipe,
                arrival=arrival,
                expiry=min(max_steps, arrival + deadline),
                resource=_ticket_resource_name(slot, recipe),
            )
        )
        idx += 1
        arrival += interarrival
    return specs


class TicketSchedule:
    """The episode's whole ticket schedule, indexed by slot.

    The slot resources are recycled, so a live `ticket_<slot>_<recipe>` on the
    board names a slot and not a ticket. This maps it back to the ticket that
    owns it at a given tick -- the last one scheduled into that slot whose
    arrival has already happened -- which is how the replay keeps reporting a
    ticket's global index and the absolute tick it expires on.
    """

    def __init__(self, specs: list[TicketSpec]) -> None:
        self.specs = list(specs)
        self._by_slot: dict[int, list[TicketSpec]] = {}
        for spec in self.specs:
            self._by_slot.setdefault(spec.slot, []).append(spec)

    def occupant(self, slot: int, step: int) -> TicketSpec | None:
        latest: TicketSpec | None = None
        for spec in self._by_slot.get(slot, ()):  # ascending arrival
            if spec.arrival > step:
                break
            latest = spec
        return latest


def resource_names_for_tickets(ticket_slots: list[str]) -> list[str]:
    return [
        *BASE_AGENT_RESOURCES,
        *PREP_PROGRESS_RESOURCES,
        *QUEUE_COUNTER_RESOURCES,
        *POT_RESOURCES,
        *FRYER_RESOURCES,
        *ticket_slots,
    ]


def kitchen_render_asset(asset_name: str, *, resources: dict[str, int] | None = None) -> RenderAsset:
    return RenderAsset(asset=asset_name, resources={} if resources is None else dict(resources))


def kitchen_render_assets() -> dict[str, list[RenderAsset]]:
    return {
        "agent": [RenderAsset(asset="scrambler")],
        "veg_station": [kitchen_render_asset("overcooked_veg_station")],
        "meat_station": [kitchen_render_asset("overcooked_meat_station")],
        "plate_station": [kitchen_render_asset("overcooked_plate_station")],
        "chopping_station": [kitchen_render_asset("overcooked_chopping_station")],
        "cooking_station": [
            kitchen_render_asset("overcooked_cooking_burned", resources={POT_SOUP_BURNED: 1}),
            kitchen_render_asset("overcooked_cooking_ready", resources={POT_SOUP_READY: 1}),
            kitchen_render_asset("overcooked_cooking_station", resources={POT_SOUP_COOKING: 1}),
            kitchen_render_asset("overcooked_cooking_station"),
        ],
        "fryer_station": [
            kitchen_render_asset("overcooked_fryer_burned", resources={FRYER_FRIES_BURNED: 1}),
            kitchen_render_asset("overcooked_fryer_ready", resources={FRYER_FRIES_READY: 1}),
            kitchen_render_asset("overcooked_fryer_station", resources={FRYER_FRIES_COOKING: 1}),
            kitchen_render_asset("overcooked_fryer_station"),
        ],
        "serving_station": [kitchen_render_asset("overcooked_serving_station")],
        "wash_station": [kitchen_render_asset("overcooked_wash_station")],
        "order_board": [kitchen_render_asset("overcooked_order_board")],
    }


def kitchen_render_config(settings: KitchenSettings) -> RenderConfig:
    return RenderConfig(
        agent_huds={
            resource: RenderHudConfig(resource=resource, short_name=short_name, max=1, rank=rank)
            for rank, (resource, short_name) in enumerate(AGENT_HUD_SPECS)
        },
        object_status={
            "agent": {
                resource: RenderStatusBarConfig(resource=resource, short_name=short_name, max=1, rank=rank)
                for rank, (resource, short_name) in enumerate(AGENT_STATUS_SPECS)
            },
            "chopping_station": {
                CHOP_VEG_PROGRESS: RenderStatusBarConfig(
                    resource=CHOP_VEG_PROGRESS, short_name="VG", max=settings.chop_ticks, rank=0
                ),
                CHOP_MEAT_PROGRESS: RenderStatusBarConfig(
                    resource=CHOP_MEAT_PROGRESS, short_name="MT", max=settings.chop_ticks, rank=1
                ),
            },
            "order_board": {
                QUEUE_SALAD: RenderStatusBarConfig(
                    resource=QUEUE_SALAD, short_name="QSD", max=settings.order_queue_max, rank=0
                ),
                QUEUE_SOUP: RenderStatusBarConfig(
                    resource=QUEUE_SOUP, short_name="QSP", max=settings.order_queue_max, rank=1
                ),
                QUEUE_FRIES: RenderStatusBarConfig(
                    resource=QUEUE_FRIES, short_name="QFR", max=settings.order_queue_max, rank=2
                ),
            },
            "cooking_station": {
                POT_SOUP_COOKING: RenderStatusBarConfig(resource=POT_SOUP_COOKING, short_name="CK", max=1, rank=0),
                POT_SOUP_READY: RenderStatusBarConfig(resource=POT_SOUP_READY, short_name="RD", max=1, rank=1),
                POT_SOUP_BURNED: RenderStatusBarConfig(resource=POT_SOUP_BURNED, short_name="BR", max=1, rank=2),
            },
            "fryer_station": {
                FRYER_FRIES_COOKING: RenderStatusBarConfig(
                    resource=FRYER_FRIES_COOKING, short_name="FC", max=1, rank=0
                ),
                FRYER_FRIES_READY: RenderStatusBarConfig(resource=FRYER_FRIES_READY, short_name="FR", max=1, rank=1),
                FRYER_FRIES_BURNED: RenderStatusBarConfig(
                    resource=FRYER_FRIES_BURNED, short_name="FB", max=1, rank=2
                ),
            },
            "wash_station": {
                WASH_PROGRESS: RenderStatusBarConfig(
                    resource=WASH_PROGRESS, short_name="WS", max=settings.wash_ticks, rank=0
                ),
            },
        },
        assets=kitchen_render_assets(),
    )


def veg_station_config() -> GridObjectConfig:
    return GridObjectConfig(
        name="veg_station",
        on_use_handler=firstMatch(
            [
                Handler(
                    name="pickup_veg",
                    filters=[isNot(actorHasAnyOf(BASE_AGENT_RESOURCES))],
                    mutations=[updateActor({VEG: 1})],
                )
            ]
        ),
    )


def meat_station_config() -> GridObjectConfig:
    return GridObjectConfig(
        name="meat_station",
        on_use_handler=firstMatch(
            [
                Handler(
                    name="pickup_meat",
                    filters=[isNot(actorHasAnyOf(BASE_AGENT_RESOURCES))],
                    mutations=[updateActor({MEAT: 1})],
                )
            ]
        ),
    )


def plate_station_config() -> GridObjectConfig:
    return GridObjectConfig(
        name="plate_station",
        on_use_handler=firstMatch(
            [
                Handler(
                    name="pickup_clean_plate",
                    filters=[isNot(actorHasAnyOf(BASE_AGENT_RESOURCES))],
                    mutations=[updateActor({CLEAN_PLATE: 1})],
                )
            ]
        ),
    )


def chopping_station_config(chop_ticks: int) -> GridObjectConfig:
    stored_ingredients = [CHOPPED_VEG, CHOPPED_MEAT]
    handlers: list[Handler] = [
        Handler(
            name="finish_chop_veg",
            filters=[targetHas({CHOP_VEG_PROGRESS: chop_ticks - 1})],
            mutations=[
                updateActor({CHOPPED_VEG: 1}),
                updateTarget({CHOP_VEG_PROGRESS: -999}),
                logActorAgentStat("veg_chopped"),
                logStatToGame("veg_chopped"),
            ],
        ),
        Handler(
            name="continue_chop_veg",
            filters=[targetHas({CHOP_VEG_PROGRESS: 1}), isNot(targetHas({CHOP_VEG_PROGRESS: chop_ticks - 1}))],
            mutations=[updateTarget({CHOP_VEG_PROGRESS: 1})],
        ),
        Handler(
            name="finish_chop_meat",
            filters=[targetHas({CHOP_MEAT_PROGRESS: chop_ticks - 1})],
            mutations=[
                updateActor({CHOPPED_MEAT: 1}),
                updateTarget({CHOP_MEAT_PROGRESS: -999}),
                logActorAgentStat("meat_chopped"),
                logStatToGame("meat_chopped"),
            ],
        ),
        Handler(
            name="continue_chop_meat",
            filters=[targetHas({CHOP_MEAT_PROGRESS: 1}), isNot(targetHas({CHOP_MEAT_PROGRESS: chop_ticks - 1}))],
            mutations=[updateTarget({CHOP_MEAT_PROGRESS: 1})],
        ),
        Handler(
            name="plate_salad",
            filters=[
                actorHas({CLEAN_PLATE: 1}),
                targetHas({CHOPPED_VEG: 1}),
                isNot(targetHas({CHOP_VEG_PROGRESS: 1})),
                isNot(targetHas({CHOP_MEAT_PROGRESS: 1})),
            ],
            mutations=[updateActor({CLEAN_PLATE: -1, DISH_SALAD: 1}), updateTarget({CHOPPED_VEG: -1})],
        ),
        Handler(
            name="store_chopped_veg",
            filters=[
                actorHas({CHOPPED_VEG: 1}),
                isNot(targetHasAnyOf(stored_ingredients)),
                isNot(targetHas({CHOP_VEG_PROGRESS: 1})),
                isNot(targetHas({CHOP_MEAT_PROGRESS: 1})),
            ],
            mutations=[updateActor({CHOPPED_VEG: -1}), updateTarget({CHOPPED_VEG: 1})],
        ),
        Handler(
            name="pickup_chopped_veg",
            filters=[
                isNot(actorHasAnyOf(BASE_AGENT_RESOURCES)),
                targetHas({CHOPPED_VEG: 1}),
                isNot(targetHas({CHOP_VEG_PROGRESS: 1})),
                isNot(targetHas({CHOP_MEAT_PROGRESS: 1})),
            ],
            mutations=[updateActor({CHOPPED_VEG: 1}), updateTarget({CHOPPED_VEG: -1})],
        ),
        Handler(
            name="start_chop_veg",
            filters=[
                actorHas({VEG: 1}),
                isNot(targetHasAnyOf(stored_ingredients)),
                isNot(targetHas({CHOP_VEG_PROGRESS: 1})),
                isNot(targetHas({CHOP_MEAT_PROGRESS: 1})),
            ],
            mutations=[updateActor({VEG: -1}), updateTarget({CHOP_VEG_PROGRESS: 1})],
        ),
        Handler(
            name="store_chopped_meat",
            filters=[
                actorHas({CHOPPED_MEAT: 1}),
                isNot(targetHasAnyOf(stored_ingredients)),
                isNot(targetHas({CHOP_VEG_PROGRESS: 1})),
                isNot(targetHas({CHOP_MEAT_PROGRESS: 1})),
            ],
            mutations=[updateActor({CHOPPED_MEAT: -1}), updateTarget({CHOPPED_MEAT: 1})],
        ),
        Handler(
            name="pickup_chopped_meat",
            filters=[
                isNot(actorHasAnyOf(BASE_AGENT_RESOURCES)),
                targetHas({CHOPPED_MEAT: 1}),
                isNot(targetHas({CHOP_VEG_PROGRESS: 1})),
                isNot(targetHas({CHOP_MEAT_PROGRESS: 1})),
            ],
            mutations=[updateActor({CHOPPED_MEAT: 1}), updateTarget({CHOPPED_MEAT: -1})],
        ),
        Handler(
            name="start_chop_meat",
            filters=[
                actorHas({MEAT: 1}),
                isNot(targetHasAnyOf(stored_ingredients)),
                isNot(targetHas({CHOP_VEG_PROGRESS: 1})),
                isNot(targetHas({CHOP_MEAT_PROGRESS: 1})),
            ],
            mutations=[updateActor({MEAT: -1}), updateTarget({CHOP_MEAT_PROGRESS: 1})],
        ),
    ]
    return GridObjectConfig(
        name="chopping_station",
        inventory=InventoryConfig(initial={CHOP_VEG_PROGRESS: 0, CHOP_MEAT_PROGRESS: 0}),
        on_use_handler=firstMatch(handlers),
    )


def cooking_station_config(soup_cook_ticks: int) -> GridObjectConfig:
    handlers: list[Handler] = [
        Handler(
            name="collect_ready_soup",
            filters=[actorHas({CLEAN_PLATE: 1}), targetHas({POT_SOUP_READY: 1})],
            mutations=[
                updateActor({CLEAN_PLATE: -1, DISH_SOUP: 1}),
                updateTarget({POT_SOUP_READY: -1, POT_READY_AGE: -999}),
            ],
        ),
        Handler(
            name="load_soup_veg_and_start",
            filters=[
                actorHas({CHOPPED_VEG: 1}),
                targetHas({CHOPPED_MEAT: 1}),
                isNot(targetHas({POT_SOUP_COOKING: 1})),
                isNot(targetHas({POT_SOUP_READY: 1})),
                isNot(targetHas({POT_SOUP_BURNED: 1})),
            ],
            mutations=[
                updateActor({CHOPPED_VEG: -1}),
                updateTarget({CHOPPED_MEAT: -1, POT_SOUP_COOKING: 1, POT_TIMER: soup_cook_ticks, POT_READY_AGE: -999}),
                logActorAgentStat("soups_started"),
                logStatToGame("soups_started"),
            ],
        ),
        Handler(
            name="load_soup_meat_and_start",
            filters=[
                actorHas({CHOPPED_MEAT: 1}),
                targetHas({CHOPPED_VEG: 1}),
                isNot(targetHas({POT_SOUP_COOKING: 1})),
                isNot(targetHas({POT_SOUP_READY: 1})),
                isNot(targetHas({POT_SOUP_BURNED: 1})),
            ],
            mutations=[
                updateActor({CHOPPED_MEAT: -1}),
                updateTarget({CHOPPED_VEG: -1, POT_SOUP_COOKING: 1, POT_TIMER: soup_cook_ticks, POT_READY_AGE: -999}),
                logActorAgentStat("soups_started"),
                logStatToGame("soups_started"),
            ],
        ),
        Handler(
            name="start_soup_cook",
            filters=[
                targetHas({CHOPPED_VEG: 1}),
                targetHas({CHOPPED_MEAT: 1}),
                isNot(targetHas({POT_SOUP_COOKING: 1})),
                isNot(targetHas({POT_SOUP_READY: 1})),
                isNot(targetHas({POT_SOUP_BURNED: 1})),
            ],
            mutations=[
                updateTarget(
                    {
                        CHOPPED_VEG: -1,
                        CHOPPED_MEAT: -1,
                        POT_SOUP_COOKING: 1,
                        POT_TIMER: soup_cook_ticks,
                        POT_READY_AGE: -999,
                    }
                ),
                logActorAgentStat("soups_started"),
                logStatToGame("soups_started"),
            ],
        ),
        Handler(
            name="load_soup_veg",
            filters=[
                actorHas({CHOPPED_VEG: 1}),
                isNot(targetHas({CHOPPED_VEG: 1})),
                isNot(targetHas({POT_SOUP_COOKING: 1})),
                isNot(targetHas({POT_SOUP_READY: 1})),
                isNot(targetHas({POT_SOUP_BURNED: 1})),
            ],
            mutations=[updateActor({CHOPPED_VEG: -1}), updateTarget({CHOPPED_VEG: 1})],
        ),
        Handler(
            name="load_soup_meat",
            filters=[
                actorHas({CHOPPED_MEAT: 1}),
                isNot(targetHas({CHOPPED_MEAT: 1})),
                isNot(targetHas({POT_SOUP_COOKING: 1})),
                isNot(targetHas({POT_SOUP_READY: 1})),
                isNot(targetHas({POT_SOUP_BURNED: 1})),
            ],
            mutations=[updateActor({CHOPPED_MEAT: -1}), updateTarget({CHOPPED_MEAT: 1})],
        ),
        Handler(
            name="clear_burned_pot",
            filters=[targetHas({POT_SOUP_BURNED: 1})],
            mutations=[
                updateTarget({POT_SOUP_BURNED: -1, POT_TIMER: -999, POT_READY_AGE: -999}),
                logActorAgentStat("pots_cleared"),
                logStatToGame("pots_cleared"),
            ],
        ),
    ]
    return GridObjectConfig(
        name="cooking_station",
        inventory=InventoryConfig(initial={POT_TIMER: 0, POT_READY_AGE: 0, CHOPPED_VEG: 0, CHOPPED_MEAT: 0}),
        on_use_handler=firstMatch(handlers),
    )


def fryer_station_config(fries_cook_ticks: int) -> GridObjectConfig:
    handlers: list[Handler] = [
        Handler(
            name="collect_ready_fries",
            filters=[actorHas({CLEAN_PLATE: 1}), targetHas({FRYER_FRIES_READY: 1})],
            mutations=[
                updateActor({CLEAN_PLATE: -1, DISH_FRIES: 1}),
                updateTarget({FRYER_FRIES_READY: -1, FRYER_READY_AGE: -999}),
            ],
        ),
        Handler(
            name="start_fries_cook",
            filters=[
                actorHas({CHOPPED_VEG: 1}),
                isNot(targetHas({FRYER_FRIES_COOKING: 1})),
                isNot(targetHas({FRYER_FRIES_READY: 1})),
                isNot(targetHas({FRYER_FRIES_BURNED: 1})),
            ],
            mutations=[
                updateActor({CHOPPED_VEG: -1}),
                updateTarget({FRYER_FRIES_COOKING: 1, FRYER_TIMER: fries_cook_ticks, FRYER_READY_AGE: -999}),
                logActorAgentStat("fries_started"),
                logStatToGame("fries_started"),
            ],
        ),
        Handler(
            name="clear_burned_fryer",
            filters=[targetHas({FRYER_FRIES_BURNED: 1})],
            mutations=[
                updateTarget({FRYER_FRIES_BURNED: -1, FRYER_TIMER: -999, FRYER_READY_AGE: -999}),
                logActorAgentStat("fryers_cleared"),
                logStatToGame("fryers_cleared"),
            ],
        ),
    ]
    return GridObjectConfig(
        name="fryer_station",
        inventory=InventoryConfig(initial={FRYER_TIMER: 0, FRYER_READY_AGE: 0}),
        on_use_handler=firstMatch(handlers),
    )


def _ticket_is_active(resource_name: str) -> GameValueFilter:
    return GameValueFilter(
        target=HandlerTarget.TARGET,
        value=QueryInventoryValue(query=ORDER_BOARD_QUERY, item=resource_name),
        min=1,
    )


def serving_station_config(ticket_slots: list[str]) -> GridObjectConfig:
    """One handler per ticket SLOT, not per prospective ticket.

    A slot's recipe is fixed by its position in the pool, so the gate is still
    "hold `dish_<recipe>` with a live ticket for that recipe on the board", and
    the serve still clears one specific ticket rather than a bare count.
    """
    handlers: list[Handler] = []
    for slot, resource in enumerate(ticket_slots):
        recipe = RECIPE_CYCLE[slot % len(RECIPE_CYCLE)]
        dish_resource = dish_resource_for_recipe(recipe)
        handlers.append(
            Handler(
                name=f"serve_ticket_{slot:03d}_{recipe}",
                filters=[actorHas({dish_resource: 1}), _ticket_is_active(resource)],
                mutations=[
                    updateActor({dish_resource: -1, DIRTY_PLATE: 1}),
                    queryDelta(
                        ORDER_BOARD_QUERY,
                        {resource: -1, queue_resource_for_recipe(recipe): -1},
                    ),
                    logActorAgentStat("orders_served"),
                    logActorAgentStat(f"orders_served_{recipe}"),
                    logStatToGame("orders_served"),
                    logStatToGame("orders_served_total"),
                    logStatToGame(f"orders_served_{recipe}"),
                ],
            )
        )
    return GridObjectConfig(name="serving_station", on_use_handler=firstMatch(handlers))


def wash_station_config(wash_ticks: int) -> GridObjectConfig:
    handlers: list[Handler] = [
        Handler(
            name="finish_wash_plate",
            filters=[targetHas({WASH_PROGRESS: wash_ticks - 1})],
            mutations=[
                updateActor({CLEAN_PLATE: 1}),
                updateTarget({WASH_PROGRESS: -999}),
                logActorAgentStat("plates_washed"),
                logStatToGame("plates_washed"),
            ],
        ),
        Handler(
            name="continue_wash_plate",
            filters=[targetHas({WASH_PROGRESS: 1}), isNot(targetHas({WASH_PROGRESS: wash_ticks - 1}))],
            mutations=[updateTarget({WASH_PROGRESS: 1})],
        ),
        Handler(
            name="start_wash_plate",
            filters=[actorHas({DIRTY_PLATE: 1}), isNot(targetHas({WASH_PROGRESS: 1}))],
            mutations=[updateActor({DIRTY_PLATE: -1}), updateTarget({WASH_PROGRESS: 1})],
        ),
    ]
    return GridObjectConfig(
        name="wash_station",
        inventory=InventoryConfig(initial={WASH_PROGRESS: 0}),
        on_use_handler=firstMatch(handlers),
    )


def order_board_config(ticket_slots: list[str], order_queue_max: int) -> GridObjectConfig:
    initial = {
        QUEUE_SALAD: 0,
        QUEUE_SOUP: 0,
        QUEUE_FRIES: 0,
        **{resource: 0 for resource in ticket_slots},
    }
    limits: dict[str, ResourceLimitsConfig] = {
        "queue_counts": ResourceLimitsConfig(
            base=order_queue_max, max=order_queue_max, resources=QUEUE_COUNTER_RESOURCES
        ),
    }
    if ticket_slots:
        limits["active_tickets"] = ResourceLimitsConfig(
            base=order_queue_max,
            max=order_queue_max,
            resources=list(ticket_slots),
        )
        limits.update(
            {
                f"ticket_{slot:03d}": ResourceLimitsConfig(base=1, max=1, resources=[resource])
                for slot, resource in enumerate(ticket_slots)
            }
        )
    return GridObjectConfig(name="order_board", inventory=InventoryConfig(initial=initial, limits=limits))


def order_events(
    ticket_specs: list[TicketSpec],
    ticket_slots: list[str],
    *,
    order_queue_max: int = ORDER_QUEUE_MAX,
) -> dict[str, EventConfig]:
    """Two events per prospective ticket, both writing its SLOT resource.

    The schedule still lays every ticket of the episode down at config time --
    the events are cheap, they carry no feature id -- but they all address the
    bounded pool. An arrival is skipped when the queue is full or when its own
    slot is still occupied; an expiry only fires while its slot is occupied.
    """
    if not ticket_specs or not ticket_slots:
        return {}
    active_tickets = SumGameValue(
        values=[QueryInventoryValue(query=ORDER_BOARD_QUERY, item=resource) for resource in ticket_slots]
    )
    events: dict[str, EventConfig] = {}
    for ticket in ticket_specs:
        events[f"ticket_arrival_{ticket.index:03d}_{ticket.recipe}"] = EventConfig(
            name=f"ticket_arrival_{ticket.index:03d}_{ticket.recipe}",
            target_query=ORDER_BOARD_QUERY,
            timesteps=[ticket.arrival],
            filters=[
                isNot(GameValueFilter(target=HandlerTarget.TARGET, value=active_tickets, min=order_queue_max)),
                isNot(targetHas({ticket.resource: 1})),
            ],
            mutations=[
                updateTarget({ticket.resource: 1, ticket.queue_resource: 1}),
                logStatToGame("orders_arrived"),
                logStatToGame(f"orders_arrived_{ticket.recipe}"),
            ],
        )
        events[f"ticket_expiry_{ticket.index:03d}_{ticket.recipe}"] = EventConfig(
            name=f"ticket_expiry_{ticket.index:03d}_{ticket.recipe}",
            target_query=ORDER_BOARD_QUERY,
            timesteps=[ticket.expiry],
            filters=[targetHas({ticket.resource: 1})],
            mutations=[
                updateTarget({ticket.resource: -1, ticket.queue_resource: -1}),
                logStatToGame("orders_expired"),
                logStatToGame(f"orders_expired_{ticket.recipe}"),
            ],
        )
    return events


def cooking_events(max_steps: int, *, soup_burn_ticks: int) -> dict[str, EventConfig]:
    return {
        "soup_cook_timer_tick": EventConfig(
            name="soup_cook_timer_tick",
            target_query=COOKING_STATION_QUERY,
            timesteps=periodic(start=0, period=1, end=max_steps),
            filters=[targetHas({POT_SOUP_COOKING: 1}), targetHas({POT_TIMER: 1})],
            mutations=[updateTarget({POT_TIMER: -1})],
        ),
        "soup_finish_cook": EventConfig(
            name="soup_finish_cook",
            target_query=COOKING_STATION_QUERY,
            timesteps=periodic(start=0, period=1, end=max_steps),
            filters=[targetHas({POT_SOUP_COOKING: 1}), isNot(targetHas({POT_TIMER: 1}))],
            mutations=[
                updateTarget({POT_SOUP_COOKING: -1, POT_SOUP_READY: 1, POT_READY_AGE: -999}),
                logStatToGame("soups_ready"),
            ],
        ),
        "soup_ready_age_tick": EventConfig(
            name="soup_ready_age_tick",
            target_query=COOKING_STATION_QUERY,
            timesteps=periodic(start=0, period=1, end=max_steps),
            filters=[targetHas({POT_SOUP_READY: 1})],
            mutations=[updateTarget({POT_READY_AGE: 1})],
        ),
        "soup_burn": EventConfig(
            name="soup_burn",
            target_query=COOKING_STATION_QUERY,
            timesteps=periodic(start=0, period=1, end=max_steps),
            filters=[targetHas({POT_SOUP_READY: 1}), targetHas({POT_READY_AGE: soup_burn_ticks})],
            mutations=[
                updateTarget({POT_SOUP_READY: -1, POT_SOUP_BURNED: 1, POT_READY_AGE: -999}),
                logStatToGame("soups_burned"),
            ],
        ),
    }


def fryer_events(max_steps: int, *, fries_burn_ticks: int) -> dict[str, EventConfig]:
    return {
        "fries_cook_timer_tick": EventConfig(
            name="fries_cook_timer_tick",
            target_query=FRYER_STATION_QUERY,
            timesteps=periodic(start=0, period=1, end=max_steps),
            filters=[targetHas({FRYER_FRIES_COOKING: 1}), targetHas({FRYER_TIMER: 1})],
            mutations=[updateTarget({FRYER_TIMER: -1})],
        ),
        "fries_finish_cook": EventConfig(
            name="fries_finish_cook",
            target_query=FRYER_STATION_QUERY,
            timesteps=periodic(start=0, period=1, end=max_steps),
            filters=[targetHas({FRYER_FRIES_COOKING: 1}), isNot(targetHas({FRYER_TIMER: 1}))],
            mutations=[
                updateTarget({FRYER_FRIES_COOKING: -1, FRYER_FRIES_READY: 1, FRYER_READY_AGE: -999}),
                logStatToGame("fries_ready"),
            ],
        ),
        "fries_ready_age_tick": EventConfig(
            name="fries_ready_age_tick",
            target_query=FRYER_STATION_QUERY,
            timesteps=periodic(start=0, period=1, end=max_steps),
            filters=[targetHas({FRYER_FRIES_READY: 1})],
            mutations=[updateTarget({FRYER_READY_AGE: 1})],
        ),
        "fries_burn": EventConfig(
            name="fries_burn",
            target_query=FRYER_STATION_QUERY,
            timesteps=periodic(start=0, period=1, end=max_steps),
            filters=[targetHas({FRYER_FRIES_READY: 1}), targetHas({FRYER_READY_AGE: fries_burn_ticks})],
            mutations=[
                updateTarget({FRYER_FRIES_READY: -1, FRYER_FRIES_BURNED: 1, FRYER_READY_AGE: -999}),
                logStatToGame("fries_burned"),
            ],
        ),
    }


def queue_instrumentation_events(max_steps: int) -> dict[str, EventConfig]:
    active_orders = SumGameValue(
        values=[
            QueryInventoryValue(query=ORDER_BOARD_QUERY, item=QUEUE_SALAD),
            QueryInventoryValue(query=ORDER_BOARD_QUERY, item=QUEUE_SOUP),
            QueryInventoryValue(query=ORDER_BOARD_QUERY, item=QUEUE_FRIES),
        ]
    )
    return {
        "queue_pressure_tick": EventConfig(
            name="queue_pressure_tick",
            target_query=ORDER_BOARD_QUERY,
            timesteps=periodic(start=0, period=1, end=max_steps),
            mutations=[
                logStatToGame("queue_samples"),
                logStatToGame(
                    "queue_salad_depth_sum",
                    source=QueryInventoryValue(query=ORDER_BOARD_QUERY, item=QUEUE_SALAD),
                ),
                logStatToGame(
                    "queue_soup_depth_sum",
                    source=QueryInventoryValue(query=ORDER_BOARD_QUERY, item=QUEUE_SOUP),
                ),
                logStatToGame(
                    "queue_fries_depth_sum",
                    source=QueryInventoryValue(query=ORDER_BOARD_QUERY, item=QUEUE_FRIES),
                ),
                logStatToGame("orders_active_sum", source=active_orders),
            ],
        ),
    }


def _agent_config() -> AgentConfig:
    # Exactly one reward term. The starter's soup/fries bonuses and its shared
    # expiry penalty are deleted so `sim.episode_rewards[i]` IS the integer
    # count of dishes seat i carried to the pass, and their sum IS the team's
    # dish count. Nothing else scores.
    rewards: dict[str, AgentReward] = {"served": reward(stat("orders_served"), weight=1.0)}
    return AgentConfig(
        inventory=InventoryConfig(
            initial={},
            limits={"carry": ResourceLimitsConfig(base=1, max=1, resources=BASE_AGENT_RESOURCES)},
        ),
        rewards=rewards,
    )


def counter_config() -> WallConfig:
    """Walls are counters: one item each, deposit and withdraw."""
    handlers: list[Handler] = []
    for resource in BASE_AGENT_RESOURCES:
        handlers.append(
            Handler(
                name=f"deposit_{resource}",
                filters=[actorHas({resource: 1}), isNot(targetHasAnyOf(BASE_AGENT_RESOURCES))],
                mutations=[deposit({resource: 1})],
            )
        )
    for resource in BASE_AGENT_RESOURCES:
        handlers.append(
            Handler(
                name=f"withdraw_{resource}",
                filters=[isNot(actorHasAnyOf(BASE_AGENT_RESOURCES)), targetHas({resource: 1})],
                mutations=[withdraw({resource: 1})],
            )
        )
    return WallConfig(
        name="wall",
        inventory=InventoryConfig(
            limits={"carry": ResourceLimitsConfig(base=1, max=1, resources=BASE_AGENT_RESOURCES)}
        ),
        on_use_handler=firstMatch(handlers),
    )


def make_env(settings: KitchenSettings) -> MettaGridConfig:
    """The MettaGridConfig for one kitchen."""
    ticket_specs = build_ticket_specs(
        settings.max_steps,
        first_arrival=settings.ticket_first_arrival,
        interarrival=settings.ticket_interarrival,
        deadline=settings.ticket_deadline,
        order_queue_max=settings.order_queue_max,
    )
    ticket_slots = ticket_slot_resources(
        ticket_slot_count(
            interarrival=settings.ticket_interarrival,
            deadline=settings.ticket_deadline,
            order_queue_max=settings.order_queue_max,
        )
    )
    game = GameConfig(
        map_builder=kitchen(settings.layout),
        max_steps=settings.max_steps,
        num_agents=settings.num_agents,
        resource_names=resource_names_for_tickets(ticket_slots),
        obs=ObsConfig(global_obs=GlobalObsConfig(local_position=True, last_action_move=True)),
        actions=ActionsConfig(
            move=MoveActionConfig(),
            noop=NoopActionConfig(),
            change_vibe=ChangeVibeActionConfig(enabled=False, vibes=[]),
        ),
        talk=TalkConfig(enabled=True, max_length=140, cooldown_steps=0),
        agents=[_agent_config() for _ in range(settings.num_agents)],
        objects={
            "wall": counter_config(),
            "veg_station": veg_station_config(),
            "meat_station": meat_station_config(),
            "plate_station": plate_station_config(),
            "chopping_station": chopping_station_config(settings.chop_ticks),
            "cooking_station": cooking_station_config(settings.soup_cook_ticks),
            "fryer_station": fryer_station_config(settings.fries_cook_ticks),
            "serving_station": serving_station_config(ticket_slots),
            "wash_station": wash_station_config(settings.wash_ticks),
            "order_board": order_board_config(ticket_slots, settings.order_queue_max),
        },
        events={
            **order_events(ticket_specs, ticket_slots, order_queue_max=settings.order_queue_max),
            **cooking_events(settings.max_steps, soup_burn_ticks=settings.soup_burn_ticks),
            **fryer_events(settings.max_steps, fries_burn_ticks=settings.fries_burn_ticks),
            **queue_instrumentation_events(settings.max_steps),
        },
        render=kitchen_render_config(settings),
    )
    env = MettaGridConfig(game=game)
    env.label = f"collab_cooking/{settings.layout}"
    return env

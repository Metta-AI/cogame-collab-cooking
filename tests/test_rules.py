"""Sim unit tests: one exact case per numbered rule in docs/rules.md.

These drive the raw simulator, not the server, so every assertion is about the
engine's own behaviour: the carry limit, the counters, the chop/wash counts,
the cook and burn timers, the serve gate and the ticket schedule.
"""

from __future__ import annotations

from collections import deque
from typing import Any

import pytest
from mettagrid.simulator import Simulator

from collab_cooking.game.game import (
    CHOP_MEAT_PROGRESS,
    CHOP_VEG_PROGRESS,
    CHOPPED_MEAT,
    CHOPPED_VEG,
    CLEAN_PLATE,
    DIRTY_PLATE,
    DISH_FRIES,
    DISH_SALAD,
    DISH_SOUP,
    FRYER_FRIES_BURNED,
    FRYER_FRIES_COOKING,
    FRYER_FRIES_READY,
    MEAT,
    POT_SOUP_BURNED,
    POT_SOUP_COOKING,
    POT_SOUP_READY,
    QUEUE_SOUP,
    TICKET_DEADLINE,
    TICKET_INTERARRIVAL,
    VEG,
    build_ticket_specs,
    ticket_slot_count,
    ticket_slot_resources,
)
from collab_cooking.kitchens.layouts import open_tiles, stations
from collab_cooking.missions.kitchen import make_kitchen_mission

LAYOUT = "open-kitchen"


def build(max_steps: int = 400, layout: str = LAYOUT) -> Any:
    sim = Simulator().new_simulation(make_kitchen_mission(layout, max_steps), seed=7)
    # Tick 0's events (the first ticket arrival) fire inside the first step.
    step_all(sim)
    return sim


def inventory_of(sim: Any, kind: str) -> dict[str, int]:
    names = list(sim.resource_names)
    for obj in sim.grid_objects().values():
        if obj.get("type_name") == kind:
            return {names[k]: int(v) for k, v in (obj.get("inventory") or {}).items() if v}
    return {}


def counter_inventory(sim: Any, pos: tuple[int, int]) -> dict[str, int]:
    names = list(sim.resource_names)
    for obj in sim.grid_objects().values():
        if obj.get("type_name") == "wall" and (int(obj["r"]), int(obj["c"])) == pos:
            return {names[k]: int(v) for k, v in (obj.get("inventory") or {}).items() if v}
    return {}


def agent_inventory(sim: Any, slot: int) -> dict[str, int]:
    # An agent's inventory is keyed by resource NAME; a grid object's is keyed
    # by resource ID. Both shapes are real, so both are handled.
    names = list(sim.resource_names)
    out: dict[str, int] = {}
    for key, value in (sim.agent(slot).inventory or {}).items():
        if not value:
            continue
        out[names[key] if isinstance(key, int) else str(key)] = int(value)
    return out


def agent_position(sim: Any, slot: int) -> tuple[int, int]:
    for obj in sim.grid_objects().values():
        if obj.get("type_name") == "agent" and obj.get("agent_id") == slot:
            return (int(obj["r"]), int(obj["c"]))
    raise AssertionError(f"agent {slot} vanished")


def step_all(sim: Any, action: str = "noop", slot: int | None = None) -> None:
    for i in range(sim.num_agents):
        sim.agent(i).set_action(action if slot is None or i == slot else "noop")
    sim.step()


MOVES = {"move_north": (-1, 0), "move_south": (1, 0), "move_west": (0, -1), "move_east": (0, 1)}


def walk_to(sim: Any, slot: int, target: tuple[int, int], limit: int = 120) -> None:
    """BFS over the kitchen's open tiles until the cog is next to `target`."""
    tiles = open_tiles(LAYOUT)
    goals = {
        (target[0] + dr, target[1] + dc) for dr, dc in MOVES.values()
    } & tiles
    for _ in range(limit):
        here = agent_position(sim, slot)
        if here in goals:
            return
        others = {agent_position(sim, i) for i in range(sim.num_agents) if i != slot}
        walkable = tiles - others
        previous: dict[tuple[int, int], tuple[int, int]] = {here: here}
        queue = deque([here])
        found = None
        while queue:
            cell = queue.popleft()
            if cell in goals:
                found = cell
                break
            for dr, dc in MOVES.values():
                nxt = (cell[0] + dr, cell[1] + dc)
                if nxt in walkable and nxt not in previous:
                    previous[nxt] = cell
                    queue.append(nxt)
        if found is None:
            step_all(sim)  # somebody is in the way; wait a tick
            continue
        cursor = found
        while previous[cursor] != here:
            cursor = previous[cursor]
        delta = (cursor[0] - here[0], cursor[1] - here[1])
        action = next(name for name, move in MOVES.items() if move == delta)
        step_all(sim, action, slot=slot)
    raise AssertionError(f"agent {slot} could not reach {target}")


def use(sim: Any, slot: int, target: tuple[int, int], times: int = 1) -> None:
    """Walk INTO the target: the move is blocked and the handler runs."""
    row, col = agent_position(sim, slot)
    if target[0] < row:
        action = "move_north"
    elif target[0] > row:
        action = "move_south"
    elif target[1] > col:
        action = "move_east"
    else:
        action = "move_west"
    for _ in range(times):
        step_all(sim, action, slot=slot)


def take(sim: Any, slot: int, station: str) -> None:
    pos = stations(LAYOUT)[station]
    walk_to(sim, slot, pos)
    use(sim, slot, pos)


def test_carry_limit_is_exactly_one_item() -> None:
    sim = build()
    take(sim, 0, "veg_station")
    assert agent_inventory(sim, 0) == {VEG: 1}
    take(sim, 0, "meat_station")
    # Already holding something: the pickup filter refuses.
    assert agent_inventory(sim, 0) == {VEG: 1}


ISLAND = (3, 4)  # a counter cell of open-kitchen's central island


def test_counter_round_trips_one_item() -> None:
    sim = build()
    take(sim, 0, "veg_station")
    assert agent_inventory(sim, 0) == {VEG: 1}
    walk_to(sim, 0, ISLAND)
    use(sim, 0, ISLAND)
    assert counter_inventory(sim, ISLAND).get(VEG, 0) == 1
    assert agent_inventory(sim, 0) == {}
    use(sim, 0, ISLAND)
    assert agent_inventory(sim, 0) == {VEG: 1}
    assert counter_inventory(sim, ISLAND).get(VEG, 0) == 0


def test_a_counter_holds_only_one_item() -> None:
    sim = build()
    take(sim, 0, "veg_station")
    walk_to(sim, 0, ISLAND)
    use(sim, 0, ISLAND)
    take(sim, 0, "meat_station")
    walk_to(sim, 0, ISLAND)
    use(sim, 0, ISLAND)
    # The counter is occupied, so the deposit filter refuses.
    assert agent_inventory(sim, 0) == {MEAT: 1}
    assert counter_inventory(sim, ISLAND).get(VEG, 0) == 1


def test_chopping_takes_three_uses_and_yields_to_the_third_user() -> None:
    sim = build()
    board = stations(LAYOUT)["chopping_station"]
    take(sim, 0, "veg_station")
    walk_to(sim, 0, board)
    use(sim, 0, board)
    assert inventory_of(sim, "chopping_station").get(CHOP_VEG_PROGRESS, 0) == 1
    assert agent_inventory(sim, 0) == {}
    use(sim, 0, board)
    assert inventory_of(sim, "chopping_station").get(CHOP_VEG_PROGRESS, 0) == 2
    use(sim, 0, board)
    assert agent_inventory(sim, 0) == {CHOPPED_VEG: 1}
    assert inventory_of(sim, "chopping_station").get(CHOP_VEG_PROGRESS, 0) == 0


def test_plating_a_salad_needs_a_clean_plate_and_chopped_veg_on_the_board() -> None:
    sim = build()
    board = stations(LAYOUT)["chopping_station"]
    take(sim, 0, "veg_station")
    walk_to(sim, 0, board)
    use(sim, 0, board, times=3)
    assert agent_inventory(sim, 0) == {CHOPPED_VEG: 1}
    use(sim, 0, board)  # stash the chopped veg
    assert inventory_of(sim, "chopping_station").get(CHOPPED_VEG, 0) == 1
    take(sim, 0, "plate_station")
    assert agent_inventory(sim, 0) == {CLEAN_PLATE: 1}
    walk_to(sim, 0, board)
    use(sim, 0, board)
    assert agent_inventory(sim, 0) == {DISH_SALAD: 1}


def chop_and_carry(sim: Any, slot: int, source: str) -> None:
    board = stations(LAYOUT)["chopping_station"]
    take(sim, slot, source)
    walk_to(sim, slot, board)
    use(sim, slot, board, times=3)


def test_pot_starts_only_with_both_chopped_ingredients_and_burns_at_ready_age() -> None:
    sim = build()
    pot = stations(LAYOUT)["cooking_station"]
    chop_and_carry(sim, 0, "veg_station")
    walk_to(sim, 0, pot)
    use(sim, 0, pot)
    # One ingredient alone only loads the pot; it does not start cooking.
    assert inventory_of(sim, "cooking_station").get(POT_SOUP_COOKING, 0) == 0
    assert inventory_of(sim, "cooking_station").get(CHOPPED_VEG, 0) == 1
    chop_and_carry(sim, 0, "meat_station")
    assert agent_inventory(sim, 0) == {CHOPPED_MEAT: 1}
    walk_to(sim, 0, pot)
    use(sim, 0, pot)
    assert inventory_of(sim, "cooking_station").get(POT_SOUP_COOKING, 0) == 1
    for _ in range(11):
        step_all(sim)
        if inventory_of(sim, "cooking_station").get(POT_SOUP_READY, 0):
            break
    assert inventory_of(sim, "cooking_station").get(POT_SOUP_READY, 0) == 1
    for _ in range(16):
        step_all(sim)
        if inventory_of(sim, "cooking_station").get(POT_SOUP_BURNED, 0):
            break
    assert inventory_of(sim, "cooking_station").get(POT_SOUP_BURNED, 0) == 1
    # One use clears the burned pot.
    walk_to(sim, 0, pot)
    use(sim, 0, pot)
    assert inventory_of(sim, "cooking_station").get(POT_SOUP_BURNED, 0) == 0


def test_fryer_cooks_in_eight_and_burns_at_eleven() -> None:
    sim = build()
    fryer = stations(LAYOUT)["fryer_station"]
    chop_and_carry(sim, 0, "veg_station")
    walk_to(sim, 0, fryer)
    use(sim, 0, fryer)
    assert inventory_of(sim, "fryer_station").get(FRYER_FRIES_COOKING, 0) == 1
    for _ in range(10):
        step_all(sim)
        if inventory_of(sim, "fryer_station").get(FRYER_FRIES_READY, 0):
            break
    assert inventory_of(sim, "fryer_station").get(FRYER_FRIES_READY, 0) == 1
    # A clean plate in hand, at the fryer, is what turns ready fries into a
    # dish. Handed over directly so the walk cannot outlast the burn timer --
    # the burn itself is the next test.
    sim.agent(0).set_inventory({CLEAN_PLATE: 1})
    step_all(sim)
    use(sim, 0, fryer)
    assert agent_inventory(sim, 0) == {DISH_FRIES: 1}


def test_fryer_burns_when_nobody_plates_it() -> None:
    sim = build()
    fryer = stations(LAYOUT)["fryer_station"]
    chop_and_carry(sim, 0, "veg_station")
    walk_to(sim, 0, fryer)
    use(sim, 0, fryer)
    for _ in range(30):
        step_all(sim)
        if inventory_of(sim, "fryer_station").get(FRYER_FRIES_BURNED, 0):
            break
    assert inventory_of(sim, "fryer_station").get(FRYER_FRIES_BURNED, 0) == 1


def make_soup(sim: Any, slot: int) -> None:
    pot = stations(LAYOUT)["cooking_station"]
    chop_and_carry(sim, slot, "veg_station")
    walk_to(sim, slot, pot)
    use(sim, slot, pot)
    chop_and_carry(sim, slot, "meat_station")
    walk_to(sim, slot, pot)
    use(sim, slot, pot)
    for _ in range(12):
        step_all(sim)
        if inventory_of(sim, "cooking_station").get(POT_SOUP_READY, 0):
            return
    raise AssertionError("the soup never became ready")


def test_the_whole_soup_chain_ends_at_the_pass() -> None:
    # The full chain, walked: fetch, chop, load, cook, plate, serve.
    sim = build()
    make_soup(sim, 0)
    pot = stations(LAYOUT)["cooking_station"]
    sim.agent(0).set_inventory({CLEAN_PLATE: 1})
    step_all(sim)
    walk_to(sim, 0, pot)
    use(sim, 0, pot)
    assert agent_inventory(sim, 0) == {DISH_SOUP: 1}


def test_serving_needs_a_live_ticket_and_credits_the_actor() -> None:
    sim = build()
    serving = stations(LAYOUT)["serving_station"]
    sim.agent(0).set_inventory({DISH_SOUP: 1})
    step_all(sim)
    walk_to(sim, 0, serving)
    live_soup = inventory_of(sim, "order_board").get(QUEUE_SOUP, 0)
    assert live_soup > 0, "the schedule puts a soup ticket up at tick 0"
    reward_before = sim.agent(0).episode_reward
    use(sim, 0, serving)
    assert agent_inventory(sim, 0) == {DIRTY_PLATE: 1}
    assert sim.agent(0).episode_reward == pytest.approx(reward_before + 1.0)
    assert inventory_of(sim, "order_board").get(QUEUE_SOUP, 0) == live_soup - 1


def test_serving_with_no_live_ticket_does_nothing() -> None:
    # Fries tickets first arrive at tick 54; before that a fries dish cannot
    # be served at all.
    sim = build()
    serving = stations(LAYOUT)["serving_station"]
    sim.agent(0).set_inventory({DISH_FRIES: 1})
    step_all(sim)
    walk_to(sim, 0, serving)
    assert inventory_of(sim, "order_board").get("queue_fries", 0) == 0
    reward_before = sim.agent(0).episode_reward
    use(sim, 0, serving, times=2)
    assert agent_inventory(sim, 0) == {DISH_FRIES: 1}
    assert sim.agent(0).episode_reward == reward_before


def test_washing_takes_three_uses() -> None:
    sim = build()
    sink = stations(LAYOUT)["wash_station"]
    # set_inventory lands on the next step, like every other mutation.
    sim.agent(0).set_inventory({DIRTY_PLATE: 1})
    step_all(sim)
    assert agent_inventory(sim, 0) == {DIRTY_PLATE: 1}
    walk_to(sim, 0, sink)
    use(sim, 0, sink)
    assert agent_inventory(sim, 0) == {}
    use(sim, 0, sink)
    assert agent_inventory(sim, 0) == {}
    use(sim, 0, sink)
    assert agent_inventory(sim, 0) == {CLEAN_PLATE: 1}


def test_tickets_arrive_every_eighteen_ticks_and_expire_fifty_later() -> None:
    specs = build_ticket_specs(900)
    assert len(specs) == 50
    assert specs[0].arrival == 0 and specs[0].recipe == "soup"
    assert [spec.arrival for spec in specs[:4]] == [0, 18, 36, 54]
    assert specs[1].expiry == specs[1].arrival + 50
    assert specs[-1].expiry <= 900
    assert [spec.recipe for spec in specs[:5]] == ["soup", "salad", "soup", "fries", "salad"]


def test_a_ticket_expires_at_arrival_plus_fifty() -> None:
    sim = build()
    assert inventory_of(sim, "order_board").get("ticket_000_soup", 0) == 1
    for _ in range(52):
        step_all(sim)
    board = inventory_of(sim, "order_board")
    assert board.get("ticket_000_soup", 0) == 0


def test_arrivals_are_skipped_once_the_queue_is_full() -> None:
    # Nobody serves, so the board saturates at order_queue_max = 8.
    sim = build(max_steps=400)
    live = 0
    for _ in range(320):
        step_all(sim)
        board = inventory_of(sim, "order_board")
        live = sum(v for k, v in board.items() if k.startswith("ticket_"))
        assert live <= 8
    assert live > 0


def test_ticket_slots_are_a_bounded_pool_recycled_across_the_episode() -> None:
    """Tickets are identified by a fixed pool of slot resources, so the
    resource list -- and mettagrid's one-byte feature-id space with it -- does
    not grow with `max_steps`. Ticket 0 and ticket 10 are different tickets
    that share slot 0, 180 ticks apart, and 180 > the 50-tick deadline, so the
    slot is always free when the next ticket is due."""
    slots = ticket_slot_count()
    assert slots == 10
    assert slots % 5 == 0, "a slot's recipe is fixed by its position in the cycle"
    assert slots > TICKET_DEADLINE // TICKET_INTERARRIVAL + 1

    specs = build_ticket_specs(900)
    assert len({spec.resource for spec in specs}) == slots
    assert specs[0].resource == specs[10].resource == "ticket_000_soup"
    assert specs[10].arrival - specs[0].arrival > TICKET_DEADLINE

    env = make_kitchen_mission(LAYOUT, 900)
    ticket_resources = [name for name in env.game.resource_names if name.startswith("ticket_")]
    assert len(ticket_resources) == slots
    assert ticket_resources == ticket_slot_resources(slots)
    # The same pool at every episode length: this is the whole invariant.
    assert len(make_kitchen_mission(LAYOUT, 120).game.resource_names) == len(env.game.resource_names)


def test_a_recycled_slot_carries_the_next_ticket_after_the_first_one_dies() -> None:
    sim = build(max_steps=400)
    assert inventory_of(sim, "order_board").get("ticket_000_soup", 0) == 1
    # Ticket 0 expires at tick 50; ticket 10 arrives into the same slot at 180.
    for _ in range(120):
        step_all(sim)
    assert inventory_of(sim, "order_board").get("ticket_000_soup", 0) == 0
    for _ in range(65):
        step_all(sim)
    assert inventory_of(sim, "order_board").get("ticket_000_soup", 0) == 1

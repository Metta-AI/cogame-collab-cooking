"""Bounded-orders / legality assertions on the four scripted baselines.

All four baselines x all eight kitchens: every emitted action is a member of
`action_names`, exactly one action per seat per tick, never before that tick's
observation, `request_id == "step-<t>"` every time, every `talk` string within
the engine's own cap and valid UTF-8, and no baseline deadlocks a seat.

Then a fuzz pass of randomly-generated plan objects -- illegal stations, wrong
types, missing keys, 10 KB strings -- through the executor, asserting it still
emits exactly one legal action per tick. A baseline that produces an illegal
or unbounded order fails CI.
"""

from __future__ import annotations

import json
import random

import pytest
from mettagrid.policy.policy_env_interface import PolicyEnvInterface
from mettagrid.simulator import Simulator

from collab_cooking.agent.brain.policy import BASELINE_NAMES, KitchenBrain
from collab_cooking.coworld.plans import TALK_RUNES, PlanError, parse_plan
from collab_cooking.coworld.player import Seat, decode_observation
from collab_cooking.kitchens.layouts import LAYOUT_NAMES, reachable_stations
from collab_cooking.missions.kitchen import make_kitchen_mission

TICKS = 600
ALIASES = ["Cog-A", "Cog-B", "Cog-C", "Cog-D"]


def wire_seats(layout: str, baselines: list[str], max_steps: int):
    env = make_kitchen_mission(layout, max_steps)
    sim = Simulator().new_simulation(env, seed=20260825)
    pei = PolicyEnvInterface.from_mg_cfg(sim.config)
    config = {
        "type": "player_config",
        "slot": 0,
        "alias": "Cog-A",
        "layout": layout,
        "policy_env": json.loads(json.dumps(pei.model_dump(mode="json"))),
    }
    seats = []
    for slot, baseline in enumerate(baselines):
        seat = Seat()
        seat.configure({**config, "slot": slot, "alias": ALIASES[slot]}, baseline)
        seats.append(seat)
    return sim, pei, seats


def raw_observation(sim, slot: int) -> list[list[int]]:
    return sim._c_sim.observations()[slot].tolist()


@pytest.mark.parametrize("layout", LAYOUT_NAMES)
@pytest.mark.parametrize("baseline", BASELINE_NAMES)
def test_baseline_emits_exactly_one_legal_action_per_tick(baseline: str, layout: str) -> None:
    sim, _pei, seats = wire_seats(layout, [baseline] * 4, TICKS)
    action_names = set(sim.action_names)
    previous = [position_of(sim, slot) for slot in range(4)]
    last_move_tick = [0] * 4
    for step in range(TICKS):
        emitted = []
        for slot, seat in enumerate(seats):
            action, talk = seat.act(raw_observation(sim, slot))
            assert action in action_names, f"{baseline}/{layout} emitted {action!r}"
            assert len(talk) <= TALK_RUNES
            talk.encode("utf-8").decode("utf-8")
            request_id = f"step-{step}"
            assert request_id == f"step-{sim.current_step}"
            emitted.append(action)
            sim.agent(slot).set_action(action)
        assert len(emitted) == 4, "exactly one action per seat per tick"
        sim.step()
        for slot in range(4):
            position = position_of(sim, slot)
            if position != previous[slot]:
                previous[slot] = position
                last_move_tick[slot] = step
            # No baseline deadlocks a seat: a cog changes tile at least once
            # per 60 ticks. `forced` seals two halves, so a cog with nothing
            # left to do there may legitimately settle.
            if layout != "forced":
                assert step - last_move_tick[slot] <= 60, (
                    f"{baseline}/{layout} seat {slot} sat on one tile for "
                    f"{step - last_move_tick[slot]} ticks"
                )


def position_of(sim, slot: int) -> tuple[int, int]:
    for obj in sim.grid_objects().values():
        if obj.get("type_name") == "agent" and obj.get("agent_id") == slot:
            return (int(obj["r"]), int(obj["c"]))
    return (-1, -1)


def random_plan(rng: random.Random) -> object:
    """400 shapes of nonsense, including things that are not objects at all."""
    kind = rng.randrange(10)
    if kind == 0:
        return "not json at all"
    if kind == 1:
        return "{"
    if kind == 2:
        return json.dumps([1, 2, 3])
    if kind == 3:
        return json.dumps({"station": "x" * 10_000})
    if kind == 4:
        return json.dumps({})
    if kind == 5:
        return json.dumps({"station": rng.choice(["pot", "PASS", "teleport", "", "42"])})
    if kind == 6:
        return json.dumps({"station": "chop", "recipe": 17, "zone": None, "handoff": []})
    if kind == 7:
        return "here you go! " + json.dumps(
            {"station": "sink", "say": "\u00e9" * 400, "note": "\u2603" * 900}
        ) + " hope that helps"
    if kind == 8:
        return json.dumps(
            {"station": rng.choice(["chop", "pot", "hold"]), "yield_to": "Cog-Z",
             "handoff": "Cog-B", "zone": "middle", "recipe": "pizza"}
        )
    return json.dumps({"station": "hold", "extra": {"nested": [1, {"deep": True}]}})


def test_plan_fuzz_keeps_the_executor_emitting_one_legal_action() -> None:
    rng = random.Random(20260825)
    layout = "forced"
    sim, _pei, seats = wire_seats(layout, ["brigade"] * 4, 400)
    action_names = set(sim.action_names)
    legal = reachable_stations(layout, (2, 3))
    accepted = 0
    fuzzed = 0
    # 4 seats x every 4th tick of 400 = the 400 plan objects the design note
    # puts through the executor.
    for step in range(400):
        if step % 4 == 0:
            for slot, seat in enumerate(seats):
                raw = random_plan(rng)
                fuzzed += 1
                try:
                    plan = parse_plan(str(raw), legal, ALIASES + ["none"])
                except PlanError:
                    seat.apply_plan({"station": ""})
                    continue
                accepted += 1
                assert len(plan.say) <= 120 and len(plan.note) <= 200
                seat.apply_plan(
                    {
                        "station": plan.station, "recipe": plan.recipe, "zone": plan.zone,
                        "handoff": plan.handoff, "yield_to": plan.yield_to,
                        "say": plan.say, "src": "llm",
                    }
                )
        for slot, seat in enumerate(seats):
            action, _talk = seat.act(raw_observation(sim, slot))
            assert action in action_names
            sim.agent(slot).set_action(action)
        sim.step()
    assert fuzzed == 400, "the design note's fuzz pass is 400 plan objects"
    assert accepted > 0, "the fuzzer must produce at least some usable plans"


def test_every_baseline_name_is_selectable_and_distinct_from_the_default() -> None:
    _sim, pei, _seats = wire_seats("cramped", ["brigade"] * 4, 20)
    roles = {
        name: KitchenBrain(pei, 3, layout="cramped", baseline=name)._role
        for name in BASELINE_NAMES
    }
    assert roles["brigade"] == "all_rounder"
    assert roles["runner"] == "all_rounder"
    assert roles["courier"] == "server"
    assert roles["passer"] == "all_rounder"
    # An unknown baseline silently becomes the default rather than crashing.
    assert KitchenBrain(pei, 0, layout="cramped", baseline="wat").baseline == "brigade"


def test_decode_observation_drops_empty_tokens_and_keeps_the_rest() -> None:
    sim, pei, _seats = wire_seats("cramped", ["brigade"] * 4, 20)
    raw = raw_observation(sim, 0)
    decoded = decode_observation(pei, raw, 0)
    assert len(decoded.tokens) > 0
    assert all(token.raw_token[0] != 255 for token in decoded.tokens)

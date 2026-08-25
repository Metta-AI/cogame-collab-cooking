"""Checklist item 2: replaying the recording reproduces every frame.

The sim is Python on a C++ mettagrid core and does not compile to wasm, so the
wasm viewer draws the arrays the game recorded rather than re-deriving them in
Nim -- the design note's §Viewer/Pipeline decision ("every frame is recorded,
not derived"), taken so the rules have one source of truth.

What makes that recording trustworthy is this test. Nothing but the replay's
own bytes goes in: the seed, the layout, the resolved config, each tick's
recorded action, and the flags that are wire facts rather than sim facts (a
plan landing, a seat being absent). Those actions are fed back through a FRESH
`Simulator` and the state it settles into is compared with the recorded state
**frame by frame** -- every cog's tile, facing action and carried item, every
station block including the omit-when-unchanged rule, every derived event in
its recorded order, and the cumulative heat map.

A frame the recorder invented, dropped or mistimed fails here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mettagrid.simulator import Simulator

from collab_cooking.coworld import replay as replay_mod
from collab_cooking.missions.kitchen import make_kitchen_mission
from tests.harness import (
    fast_cert_config,
    prompt_registration,
    run_episode,
    scripted_registration,
)

BLOCKED_BIT = 1


@pytest.fixture(scope="module")
def document(tmp_path_factory) -> dict:
    """One real episode, read back as the bytes the viewer would be served."""
    out = run_episode(
        fast_cert_config(
            max_steps=240,
            step_seconds=0.0,
            shutdown_grace_seconds=0.0,
            player_connect_timeout_seconds=2,
            plan_interval_steps=60,
            min_plan_interval_seconds=0.0,
        ),
        # A prompt seat with no credentials contributes `fallback` events: the
        # replay carries events the sim cannot produce as well as those it can,
        # and the re-derivation has to keep them apart.
        [
            prompt_registration("Keep the pot busy."),
            scripted_registration("brigade"),
            scripted_registration("passer"),
            scripted_registration("courier"),
        ],
        Path(tmp_path_factory.mktemp("rederive")),
    )
    return json.loads(out["replay_bytes"].decode("utf-8"))


def rebuild(document: dict) -> replay_mod.ReplayWriter:
    """Replay the recorded actions through a fresh sim, recording as we go."""
    config = document["config"]
    env = make_kitchen_mission(
        config["layout"], config["max_steps"], num_agents=config["num_agents"]
    )
    sim = Simulator().new_simulation(env, seed=document["seed"])
    num_agents = int(config["num_agents"])
    aliases = [seat["alias"] for seat in document["seats"]]

    writer = replay_mod.ReplayWriter(
        layout=document["layout"],
        seed=document["seed"],
        config=config,
        seats=document["seats"],
        generated_at=document["generated_at"],
    )
    previous = replay_mod.capture(sim, num_agents)
    for record in document["ticks"]:
        actions = [str(entry[3]) for entry in record["c"]]
        for slot, name in enumerate(actions):
            sim.agent(slot).set_action(name)
        sim.step()
        current = replay_mod.capture(sim, num_agents)
        events = replay_mod.derive_events(previous, current, actions, aliases, writer.heat)
        # Bit 1 (last move blocked) is re-derived from the sim; bits 2 and 4 (a
        # plan landed, the seat is absent) are wire facts the sim cannot know,
        # so they are read back from the recording.
        flags = []
        for slot, entry in enumerate(record["c"]):
            bits = int(entry[4]) & ~BLOCKED_BIT
            if actions[slot].startswith("move_") and not current.cogs[slot]["success"]:
                bits |= BLOCKED_BIT
            flags.append(bits)
        # Events the sim cannot derive (episode_start, plan, fallback,
        # deadline, episode_end) are the ones the server injects out of band:
        # `episode_start` is buffered and prepended to the first record, and
        # everything else is appended to the record it lands on
        # (`live_episode._push_event`). Keep them where they were recorded; the
        # derived events between them are the ones under test.
        recorded_events = record.get("ev", [])
        first = next(
            (i for i, e in enumerate(recorded_events) if e["ev"] in replay_mod.DIFF_ORDER),
            len(recorded_events),
        )
        head = [e for e in recorded_events[:first] if e["ev"] not in replay_mod.DIFF_ORDER]
        tail = [e for e in recorded_events[first:] if e["ev"] not in replay_mod.DIFF_ORDER]
        writer.append(current, actions, flags, [*head, *events, *tail])
        previous = current
    return writer


def test_replaying_the_recorded_actions_reproduces_every_tick(document: dict) -> None:
    rebuilt = rebuild(document).ticks
    recorded = document["ticks"]
    assert len(rebuilt) == len(recorded)
    for mine, theirs in zip(rebuilt, recorded, strict=True):
        assert mine["t"] == theirs["t"]
        # Cog tiles, carried items, actions and flags, every tick.
        assert mine["c"] == theirs["c"], f"tick {theirs['t']}: cogs diverge"
        # Station state, including the "absent means identical to the previous
        # tick" rule -- an `st` block present in one and not the other fails.
        assert ("st" in mine) == ("st" in theirs), f"tick {theirs['t']}: st presence diverges"
        assert mine.get("st") == theirs.get("st"), f"tick {theirs['t']}: stations diverge"
        assert mine["sc"] == theirs["sc"], f"tick {theirs['t']}: scores diverge"
        assert mine.get("ev", []) == theirs.get("ev", []), f"tick {theirs['t']}: events diverge"


def test_the_re_derivation_is_not_vacuous(document: dict) -> None:
    """A frame-by-frame comparison proves nothing if the frames are empty."""
    ticks = document["ticks"]
    assert len(ticks) == 240
    assert sum(len(tick.get("ev", [])) for tick in ticks) > 100
    assert sum(1 for tick in ticks if "st" in tick) > 100
    carried = {entry[2] for tick in ticks for entry in tick["c"]}
    assert len(carried) > 2, carried


def test_the_heat_map_is_reproduced_too(document: dict) -> None:
    rebuilt = rebuild(document)
    assert sorted([x, y, n] for (x, y), n in rebuilt.heat.items()) == document["heat"]
    assert document["heat"], "the fixture must actually block some moves"


def test_a_tampered_recording_is_caught(document: dict) -> None:
    """The comparison is load-bearing: move one cog one tile and it fails."""
    tampered = json.loads(json.dumps(document))
    for tick in tampered["ticks"][120:]:
        tick["c"][0][0] += 1
    with pytest.raises(AssertionError):
        test_replaying_the_recorded_actions_reproduces_every_tick(tampered)


def test_a_dropped_event_is_caught(document: dict) -> None:
    """And so is a derived event the recorder failed to write."""
    tampered = json.loads(json.dumps(document))
    for tick in tampered["ticks"]:
        derived = [e for e in tick.get("ev", []) if e["ev"] in replay_mod.DIFF_ORDER]
        if derived:
            tick["ev"] = [e for e in tick["ev"] if e is not derived[0]]
            break
    else:  # pragma: no cover - the fixture always derives events
        pytest.fail("the fixture derived no events at all")
    with pytest.raises(AssertionError):
        test_replaying_the_recorded_actions_reproduces_every_tick(tampered)

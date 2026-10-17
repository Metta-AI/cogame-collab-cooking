"""Complete shift-order games through the real parser, executor and simulator."""

from __future__ import annotations

import hashlib
import json

import pytest

from collab_cooking.coworld.plans import PlanError
from collab_cooking.coworld.prompts import HELDOUT_VARIANTS, TRAINING_VARIANTS
from collab_cooking.kitchens.layouts import LAYOUT_NAMES
from tools.plan_bridge import PlanBridge


@pytest.mark.parametrize("variant", LAYOUT_NAMES)
def test_full_matched_shift_games(variant):
    selections = [(wording, 0) for wording in (*TRAINING_VARIANTS, *HELDOUT_VARIANTS)]
    selections += [("mixed", seed) for seed in range(5)]
    canonical = None
    systems = set()
    bridge = PlanBridge(variant)
    for wording, prompt_seed in selections:
        command = {
            "kind": "reset",
            "players": 4,
            "seed": "prompt-panel-17",
            "promptVariant": wording,
            "promptSeed": prompt_seed,
            "split": "test" if wording in HELDOUT_VARIANTS else "train",
        }
        if wording == "canonical":
            for heldout in HELDOUT_VARIANTS:
                with pytest.raises(ValueError, match="evaluation-only"):
                    bridge.handle({**command, "promptVariant": heldout})
            with pytest.raises(ValueError):
                bridge.handle({**command, "promptSeed": True})
        observation = bridge.handle(command)
        trace = []
        while observation["kind"] == "decision":
            seat = observation["seat"]
            assert seat == len(trace) % 4
            if not trace:
                with pytest.raises(PlanError):
                    bridge.handle(
                        {
                            "kind": "step",
                            "decision_id": observation["decision_id"],
                            "response": '{"station":"not-a-station"}',
                        }
                    )
            view = observation["semantic_view"].copy()
            metadata = view.pop("prompt_template")
            assert metadata["bank"] == "collab-cooking-rules-v1"
            assert metadata["variant"] == (TRAINING_VARIANTS[prompt_seed] if wording == "mixed" else wording)
            for message in observation["messages"]:
                assert metadata[message["role"] + "_sha256"] == hashlib.sha256(message["content"].encode()).hexdigest()
                for other in range(4):
                    if other != seat:
                        assert f"PRIVATE-SEAT-{other}" not in message["content"]
            system = observation["messages"][0]["content"]
            systems.add(system.split("Orders arrive", 1)[0])
            assert "0.01 times the dishes you personally delivered" in system
            assert "STANDING ORDERS" not in system
            proposal = json.loads(bridge.handle({"kind": "teacher"})["response"])
            proposal["note"] = f"PRIVATE-SEAT-{seat}:" + "雪" * 240
            proposal["say"] = "hello " + "雪" * 130
            result = bridge.handle(
                {"kind": "step", "decision_id": observation["decision_id"], "response": json.dumps(proposal)}
            )
            assert result["kind"] == "accepted"
            assert len(result["action"]["note"]) == 200 and len(result["action"]["say"]) == 120
            trace.append((view, observation["messages"][1], system.split("Orders arrive", 1)[1], result["action"]))
            observation = result["observation"]
        assert len(trace) == 72 and bridge.episode.sim.current_step == 900
        delivered = bridge.episode.delivered()
        assert observation["scores"] == {str(slot): sum(delivered) + 0.01 * own for slot, own in enumerate(delivered)}
        outcome = (trace, observation)
        if wording == "canonical":
            canonical = outcome
        else:
            assert outcome == canonical
    assert len(systems) == 28  # Seven wordings for four stable seat aliases.

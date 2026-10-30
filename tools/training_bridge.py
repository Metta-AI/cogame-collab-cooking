"""Metta JSONL bridge over the hosted kitchen's ordinary player observations and actions."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from collab_cooking.agent.brain.policy import KitchenBrain
from collab_cooking.coworld.live_episode import EpisodeConfig, LiveMettaGridEpisode
from collab_cooking.coworld.observation import decode_observation
from collab_cooking.missions.kitchen import make_kitchen_mission

BASELINES = ("brigade", "brigade", "passer", "courier")


def decision(
    observation: dict[str, Any],
    decision_id: int,
    seat: int,
    actions: list[str],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    candidates = {
        str(index): {"decision": {"action": name}, "criterion": {"action": name}} for index, name in enumerate(actions)
    }
    return {
        "kind": "decision",
        "game": "collab_cooking",
        "inference_mode": "physical_choice_research",
        "decision_id": decision_id,
        "seat": seat,
        "engine_seat": seat,
        "turn": observation["step"],
        "semantic_view": observation,
        "inbox": [],
        "messages": [],
        "speech_messages": [],
        "action_schema": {"enum": [{"action": name} for name in actions]},
        "typed_question": {
            "state": {"frame": observation, "metadata": metadata},
            "instructions": "Choose the next legal kitchen action to maximize team dishes served.",
            "candidates": candidates,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", default="open-kitchen")
    parser.add_argument(
        "--research-physical",
        action="store_true",
        required=True,
        help="Explicit physical-choice research profile; distinct from hosted native macro inference",
    )
    parser.add_argument("--steps", type=int)
    args = parser.parse_args()
    manifest = json.loads((Path(__file__).resolve().parents[1] / "coworld_manifest_template.json").read_text())
    variant = next(v for v in manifest["variants"] if v["id"] == args.variant)
    config = EpisodeConfig.from_mapping(variant["game_config"])
    if args.steps is not None:
        if args.steps < 1:
            raise ValueError("--steps must be positive")
        config.max_steps = args.steps

    episode: LiveMettaGridEpisode | None = None
    players: list[KitchenBrain] = []
    states: list[Any] = []
    observations: list[dict[str, Any]] = []
    pending: list[str] = []
    metadata: dict[str, Any] = {}
    decision_id = 0
    seat = 0
    teacher_action: dict[str, str] | None = None

    for line in sys.stdin:
        request = json.loads(line)
        kind = request["kind"]
        if kind == "reset":
            if request["players"] != 4:
                raise ValueError("Collaborative Cooking requires four players")
            config.seed = int.from_bytes(hashlib.sha256(request["seed"].encode()).digest()[:4], "big")
            env = make_kitchen_mission(config.layout, config.max_steps, num_agents=4)
            episode = LiveMettaGridEpisode.from_env(
                env,
                config=config,
                tokens=[f"training-{i}" for i in range(4)],
                player_names=[f"teacher-{i}" for i in range(4)],
            )
            players = []
            states = []
            for slot, baseline in enumerate(BASELINES):
                player = KitchenBrain(episode.policy_env, slot, layout=config.layout, baseline=baseline)
                players.append(player)
                states.append(player.initial_agent_state())
            observations = [
                {
                    "type": "observation",
                    "slot": slot,
                    "step": int(episode.sim.current_step),
                    "observation": episode.private_observation(slot),
                    "scores": episode.scores(),
                }
                for slot in range(4)
            ]
            metadata = episode.observation_metadata()
            pending = []
            decision_id = 0
            seat = 0
            teacher_action = None
        elif episode is None:
            raise ValueError("reset before other commands")

        if kind == "reset":
            response = decision(observations[seat], decision_id, seat, episode.action_names, metadata)
        elif kind == "encode":
            observation = observations[seat]
            values = [int(slot == seat) for slot in range(4)]
            values.extend((observation["step"], config.max_steps))
            values.extend(observation["scores"])
            values.extend(value for token in observation["observation"] for value in token)
            response = {
                "decision_id": decision_id,
                "values": values,
                "actions": [{"action": name} for name in episode.action_names],
            }
            print(json.dumps(response), flush=True)
            continue
        elif kind == "teacher":
            if teacher_action is None:
                observed = decode_observation(episode.policy_env, observations[seat]["observation"], seat)
                proposed, states[seat] = players[seat].step_with_state(observed, states[seat])
                teacher_action = {"action": proposed.name}
            print(json.dumps({"response": json.dumps(teacher_action)}), flush=True)
            continue
        elif kind == "step":
            if request["decision_id"] != decision_id:
                raise ValueError("stale decision")
            action = json.loads(request["response"])
            if set(action) != {"action"} or action["action"] not in episode.action_names:
                raise ValueError("action is not offered")
            pending.append(action["action"])
            decision_id += 1
            teacher_action = None
            if len(pending) == 4:
                for slot, name in enumerate(pending):
                    episode.sim.agent(slot).set_action(name)
                episode.sim.step()
                pending = []
                if episode.sim.is_done() or episode.sim.current_step >= config.max_steps:
                    delivered = episode.delivered()
                    observation = {
                        "kind": "terminal",
                        "scores": {str(slot): sum(delivered) + 0.01 * count for slot, count in enumerate(delivered)},
                    }
                else:
                    observations = [
                        {
                            "type": "observation",
                            "slot": slot,
                            "step": int(episode.sim.current_step),
                            "observation": episode.private_observation(slot),
                            "scores": episode.scores(),
                        }
                        for slot in range(4)
                    ]
                    seat = 0
                    observation = decision(observations[seat], decision_id, seat, episode.action_names, metadata)
            else:
                seat = len(pending)
                observation = decision(observations[seat], decision_id, seat, episode.action_names, metadata)
            response = {"kind": "accepted", "action": action, "observation": observation}
        else:
            raise ValueError(f"unknown command: {kind}")
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()

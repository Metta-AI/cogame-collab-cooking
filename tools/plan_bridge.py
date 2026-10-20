"""Synchronous shift-order collection using the published simulator and executor.

Each phase freezes four real seat views, queues four parsed plans, then runs the
ordinary player executors until the next planning boundary. Native wall-clock
latency, retry and rate-budget effects are measured separately by the hosted path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from uuid import uuid4

from collab_cooking.coworld import replay
from collab_cooking.coworld.live_episode import EpisodeConfig, LiveMettaGridEpisode
from collab_cooking.coworld.macro_executor import MacroExecutor
from collab_cooking.coworld.plan_protocol import (
    DirectiveInstalled,
    NativeProfile,
    PhysicalAction,
    PhysicalObservation,
    PlanControl,
)
from collab_cooking.coworld.plans import parse_plan
from collab_cooking.coworld.prompts import HELDOUT_VARIANTS, PromptSelection
from collab_cooking.coworld.results import seat_scores
from collab_cooking.missions.kitchen import mission_for_config


class PlanBridge:
    def __init__(self, variant: str):
        manifest = json.loads((Path(__file__).resolve().parents[1] / "coworld_manifest_template.json").read_text())
        self.config = EpisodeConfig.from_mapping(
            next(v["game_config"] for v in manifest["variants"] if v["id"] == variant)
        )
        self.decision_id = 0
        self.pending: list[PlanControl] = []
        self.players: list[MacroExecutor] = []

    def advance(self):
        episode = self.episode
        while episode.sim.current_step < self.config.max_steps and not episode.sim.is_done():
            step = int(episode.sim.current_step)
            actions = []
            for slot, player in enumerate(self.players):
                observation = PhysicalObservation(
                    request_id=uuid4(),
                    slot=slot,
                    step=step,
                    observation=episode.private_observation(slot),
                )
                player.issue(observation)
                control = player.admit(
                    PhysicalAction(
                        request_id=observation.request_id,
                        slot=slot,
                        action_name=player.expected,
                    )
                )
                actions.append(control.installed_action)
            for slot, action in enumerate(actions):
                episode.sim.agent(slot).set_action(action)
            episode.sim.step()
            current = replay.capture(episode.sim, 4)
            events = replay.derive_events(
                episode.state, current, actions, [s.alias for s in episode.seats], episode.heat
            )
            episode._absorb(events)
            episode.state = current
            episode._update_prompt_memory()
            if step % self.config.plan_interval_steps == 0:
                episode.turn += 1
                self.requests = [episode.private_window(seat.slot, NativeProfile()) for seat in episode.seats]
                self.views = [episode._seat_view(seat) for seat in episode.seats]
                return self.observation()
        return {
            "kind": "terminal",
            "scores": {str(slot): score for slot, score in enumerate(seat_scores(episode.delivered()))},
        }

    def observation(self):
        seat = len(self.pending)
        request = self.requests[seat]
        view = {
            **self.views[seat],
            "prompt_template": self.episode.prompt_selection.metadata(request.system, request.user),
        }
        return {
            "kind": "decision",
            "game": "collab-cooking",
            "decision_id": self.decision_id,
            "seat": seat,
            "engine_seat": seat,
            "turn": self.episode.turn,
            "inference_mode": "text_action",
            "semantic_view": view,
            "inbox": [],
            "messages": [{"role": "system", "content": request.system}, {"role": "user", "content": request.user}],
            "speech_messages": [],
            "typed_question": None,
            "action_schema": {
                "type": "object",
                "required": ["station"],
                "properties": {
                    "station": {"type": "string", "enum": request.legal_stations},
                    "recipe": {"type": "string", "enum": ["salad", "soup", "fries", "any"]},
                    "zone": {"type": "string", "enum": ["left", "right", "pass", "any"]},
                    "handoff": {"type": "string", "enum": request.ally_aliases},
                    "yield_to": {"type": "string", "enum": request.ally_aliases},
                    "say": {"type": "string", "maxLength": 120},
                    "note": {"type": "string", "maxLength": 200},
                },
            },
        }

    def handle(self, command):
        match command["kind"]:
            case "reset":
                selection = PromptSelection.model_validate(command)
                split = command["split"] if "split" in command else "train"
                if split not in ("train", "validation", "test"):
                    raise ValueError("split must be train, validation, or test")
                if selection.selected in HELDOUT_VARIANTS and split == "train":
                    raise ValueError("held-out wording is evaluation-only")
                if command["players"] != 4:
                    raise ValueError("Collaborative Cooking requires four seats")
                self.config.seed = int.from_bytes(hashlib.sha256(command["seed"].encode()).digest()[:4], "big")
                self.episode = LiveMettaGridEpisode.from_env(
                    mission_for_config(self.config),
                    config=self.config,
                    tokens=[f"training-{i}" for i in range(4)],
                    player_names=[f"teacher-{i}" for i in range(4)],
                    prompt_selection=selection,
                )
                self.players = []
                for slot, seat in enumerate(self.episode.seats):
                    seat.kind = "prompt"
                    seat.connected = seat.ever_connected = seat.registered = True
                    player = MacroExecutor(self.episode.policy_env, slot, self.config.layout, "brigade")
                    self.players.append(player)
                self.episode.state = replay.capture(self.episode.sim, 4)
                self.decision_id = 0
                self.pending = []
                return self.advance()
            case "teacher":
                # A deterministic legal-plan fixture, not an expert teacher.
                request = self.requests[len(self.pending)]
                stations = [station for station in request.legal_stations if station != "hold"]
                station = stations[(self.episode.turn + request.slot) % len(stations)] if stations else "hold"
                return {
                    "response": json.dumps(
                        {
                            "station": station,
                            "recipe": "any",
                            "zone": "any",
                            "handoff": "none",
                            "yield_to": "none",
                            "say": "",
                            "note": "",
                        }
                    )
                }
            case "step":
                if command["decision_id"] != self.decision_id:
                    raise ValueError("stale decision")
                request = self.requests[len(self.pending)]
                plan = parse_plan(command["response"], request.legal_stations, request.ally_aliases)
                self.pending.append(plan)
                self.decision_id += 1
                if len(self.pending) < 4:
                    observation = self.observation()
                else:
                    for slot, queued in enumerate(self.pending):
                        seat = self.episode.seats[slot]
                        seat.say, seat.note = queued.say, queued.note
                        seat.last_order = f"station={queued.station} recipe={queued.recipe} zone={queued.zone} handoff={queued.handoff}"
                        self.players[slot].install(
                            DirectiveInstalled(
                                window_id=self.requests[slot].window_id,
                                slot=slot,
                                turn=self.episode.turn,
                                plan=queued,
                                cause="accepted",
                            )
                        )
                    self.pending = []
                    observation = self.advance()
                return {"kind": "accepted", "action": plan.model_dump(mode="json"), "observation": observation}
            case _:
                raise ValueError("unknown bridge command")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", default="open-kitchen")
    bridge = PlanBridge(parser.parse_args().variant)
    for line in sys.stdin:
        print(json.dumps(bridge.handle(json.loads(line))), flush=True)


if __name__ == "__main__":
    main()

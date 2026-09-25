"""Jev shift choices over Collaborative Cooking's ordinary player socket."""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Literal

from pydantic import BaseModel

from collab_cooking.agent.brain.policy import PlanDirective
from collab_cooking.coworld.plans import TALK_RUNES, truncate_runes
from collab_cooking.coworld.player import Seat
from collab_cooking.coworld.player import main as player_main
from collab_cooking.kitchens.layouts import reachable_stations

STATION_CRITERIA = {
    "veg": "Fetch vegetables for the current prep queue.",
    "meat": "Fetch meat for soup prep.",
    "chop": "Chop ingredients for queued dishes.",
    "pot": "Load or serve soup before it burns.",
    "fryer": "Load or serve fries before they burn.",
    "plate": "Fetch a clean plate for a ready dish.",
    "pass": "Carry a finished dish to the pass for a live ticket.",
    "sink": "Wash a dirty plate for reuse.",
    "board": "Stage ingredients at the order board.",
    "hold": "Stay in place until the next shift choice.",
}


class ChoiceAnswer(BaseModel):
    type: Literal["choice"]
    probabilities: dict[str, float]


class SystemOneResponse(BaseModel):
    answers: dict[str, ChoiceAnswer]


class JevSeat(Seat):
    """Keep acting every tick while a model ranks the next shift's jobs."""

    def __init__(self) -> None:
        super().__init__()
        sidecar = os.environ.get("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", "").strip()
        capture = os.environ.get("METTA_CAPTURE_URL", "").strip()
        if sidecar:
            self.base_url = sidecar.rstrip("/")
            self.api_key = ""
            self.model = "typesafe/jev-1.13"
        elif capture:
            self.base_url = capture.rstrip("/")
            self.api_key = os.environ["METTA_CAPTURE_KEY"]
            self.model = os.environ.get("METTA_CAPTURE_MODEL", "jev-latest")
        else:
            self.base_url = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/")
            self.api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
            self.model = os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest")
        self.enabled = bool(sidecar or capture or self.api_key)
        if not self.enabled:
            print("collab-cooking Jev: no model route; following brigade", file=sys.stderr, flush=True)
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.pending: Future[str] | None = None
        self.calls = 0
        self.choice = "baseline"

    def _query(self, state: dict[str, Any], criteria: dict[str, str]) -> str:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            headers["x-coworld-player-slot"] = str(self.slot)
        payload = {
            "model": self.model,
            "state": json.dumps(state, separators=(",", ":")),
            "questions": {
                "shift": {
                    "type": "choice",
                    "instructions": "Choose one job for this cog until the next shift. Maximize team dishes served. "
                    "Use only this seat's remembered observations; stale station sightings may have changed.",
                    "criteria": criteria,
                }
            },
        }
        request = urllib.request.Request(
            f"{self.base_url}/v1/systemone",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - configured model endpoint
            result = SystemOneResponse.model_validate_json(response.read())
        probabilities = result.answers["shift"].probabilities
        if (
            set(probabilities) != set(criteria)
            or any(not math.isfinite(value) or value < 0 or value > 1 for value in probabilities.values())
            or abs(sum(probabilities.values()) - 1) > 0.01
        ):
            raise ValueError("Jev returned invalid shift probabilities")
        return max(probabilities, key=probabilities.__getitem__)

    def observe(self, message: dict[str, Any]) -> tuple[str, str]:
        if self.pending is not None and self.pending.done():
            error = self.pending.exception()
            if error is None:
                self.choice = self.pending.result()
                if self.choice == "baseline":
                    self.brain.apply_plan(self.state, None)
                else:
                    self.brain.apply_plan(
                        self.state,
                        PlanDirective(turn=message["step"] // 50, station=self.choice, src="jev"),
                    )
                print(
                    f"collab-cooking Jev slot={self.slot} step={message['step']} shift={self.choice}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                self.brain.apply_plan(self.state, None)
                print(f"collab-cooking Jev slot={self.slot} model error: {error}", file=sys.stderr, flush=True)
            self.pending = None

        action_name, task = self.act(message["observation"])
        step = message["step"]
        if self.enabled and step % 50 == 0 and self.pending is None and self.calls < 18:
            position = self.brain.to_absolute(self.state.position)
            legal = reachable_stations(self.layout, position)
            criteria = {"baseline": "Follow the brigade baseline's role and immediate task choices."}
            criteria.update({station: STATION_CRITERIA[station] for station in legal})
            known = [
                {
                    "position": self.brain.to_absolute(where),
                    "type": entity.type,
                    "properties": entity.properties,
                    "age": self.state.step - entity.last_seen,
                }
                for where, entity in self.state.entity_map.entities.items()
                if entity.type != "wall"
            ]
            state = {
                "game": "Collaborative Cooking",
                "goal": "Serve as many live dish tickets as the four-cog team can.",
                "layout": self.layout,
                "seat": self.alias,
                "step": step,
                "position": position,
                "carrying": self.state.inventory,
                "current_task": self.state.current_task,
                "scores": message["scores"],
                "known_entities": known,
            }
            self.pending = self.executor.submit(self._query, state, criteria)
            self.calls += 1
        return action_name, truncate_runes(f"{self.choice}: {task}", TALK_RUNES)


def external_registration() -> dict[str, str]:
    return {"type": "register", "kind": "external"}


if __name__ == "__main__":
    player_main(JevSeat, external_registration)

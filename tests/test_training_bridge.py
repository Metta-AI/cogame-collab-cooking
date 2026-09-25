"""The training bridge plays through the same seat actions as the hosted player."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from collab_cooking.coworld.live_episode import EpisodeConfig

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "coworld_manifest_template.json").read_text())


def request(process: subprocess.Popen[str], message: dict) -> dict:
    assert process.stdin is not None and process.stdout is not None
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()
    return json.loads(process.stdout.readline())


def play(variant: str, steps: int) -> dict:
    with subprocess.Popen(
        [sys.executable, str(ROOT / "tools/training_bridge.py"), "--variant", variant, "--steps", str(steps)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as process:
        observation = request(process, {"kind": "reset", "seed": "test-1", "players": 4})
        decisions = 0
        while observation["kind"] != "terminal":
            assert observation["decision_id"] == decisions
            assert observation["seat"] == decisions % 4
            assert observation["semantic_view"]["step"] == decisions // 4
            assert observation["semantic_view"]["type"] == "observation"
            assert len(observation["semantic_view"]["observation"]) == 500
            question = observation["typed_question"]
            assert question["state"]["frame"] == observation["semantic_view"]
            assert len(question["state"]["metadata"]["features"]) > 0
            assert set(question["candidates"]) == {"0", "1", "2", "3", "4"}
            encoding = request(process, {"kind": "encode"})
            assert len(encoding["values"]) == 1510
            assert len(encoding["actions"]) == 5
            teacher = json.loads(request(process, {"kind": "teacher"})["response"])
            assert teacher in encoding["actions"]
            assert teacher in [candidate["decision"] for candidate in question["candidates"].values()]
            result = request(
                process,
                {
                    "kind": "step",
                    "decision_id": decisions,
                    "response": json.dumps(teacher),
                },
            )
            assert result["kind"] == "accepted"
            observation = result["observation"]
            decisions += 1
        assert decisions == 4 * steps
        process.stdin.close()
        assert process.wait() == 0
        return observation["scores"]


def test_all_published_kitchens_accept_scripted_player_actions() -> None:
    for variant in MANIFEST["variants"]:
        scores = play(variant["id"], 12)
        assert all(score >= 0 for score in scores.values())


def test_complete_kitchen_game_returns_the_published_team_score() -> None:
    scores = play("cramped", EpisodeConfig().max_steps)
    assert all(score >= 0 for score in scores.values())
    dishes = int(min(scores.values()))
    assert dishes > 0
    assert all(dishes <= score < dishes + 0.5 for score in scores.values())

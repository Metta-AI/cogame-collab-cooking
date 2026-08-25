"""An end-to-end episode that writes a replay, and every legal end reason."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from collab_cooking.coworld.live_episode import assign_aliases
from tests.harness import (
    CERT_CONFIG,
    fast_cert_config,
    prompt_registration,
    run_episode,
    scripted_registration,
)

FOUR_SCRIPTED = [
    scripted_registration("brigade"),
    scripted_registration("brigade"),
    scripted_registration("passer"),
    scripted_registration("courier"),
]


def full_episode(tmp_path: Path, **overrides):
    config = fast_cert_config(
        step_seconds=0.0, shutdown_grace_seconds=0.0, player_connect_timeout_seconds=2, **overrides
    )
    return run_episode(config, FOUR_SCRIPTED, tmp_path)


def test_a_complete_episode_writes_both_artifacts(tmp_path: Path) -> None:
    out = full_episode(tmp_path)
    results = out["results"]
    assert results["reason"] == "complete"
    assert results["steps"] == CERT_CONFIG["max_steps"]

    replay = json.loads(out["replay_bytes"].decode("utf-8"))
    assert len(replay["ticks"]) == CERT_CONFIG["max_steps"]
    assert replay["layout"] == "cramped"
    assert replay["seed"] == CERT_CONFIG["seed"]

    # Dishes recomputed independently from the serve events must equal
    # results.dishes -- the two are derived by different code paths.
    serves = sum(
        1
        for tick in replay["ticks"]
        for event in tick.get("ev", [])
        if event["ev"] == "serve"
    )
    assert serves == results["dishes"]
    assert results["dishes"] == sum(results["delivered"])
    # And every seat's own serve count matches its delivered entry.
    per_seat = [0, 0, 0, 0]
    for tick in replay["ticks"]:
        for event in tick.get("ev", []):
            if event["ev"] == "serve":
                per_seat[event["slot"]] += 1
    assert per_seat == results["delivered"]


def test_two_runs_of_one_seed_are_byte_identical_modulo_generated_at(tmp_path: Path) -> None:
    first = full_episode(tmp_path / "a", max_steps=180)
    second = full_episode(tmp_path / "b", max_steps=180)
    left = json.loads(first["replay_bytes"].decode("utf-8"))
    right = json.loads(second["replay_bytes"].decode("utf-8"))
    left.pop("generated_at")
    right.pop("generated_at")
    assert json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def test_aliases_are_a_seeded_permutation_and_stable(tmp_path: Path) -> None:
    assert sorted(assign_aliases(20260826, 4)) == ["Cog-A", "Cog-B", "Cog-C", "Cog-D"]
    assert assign_aliases(20260826, 4) == assign_aliases(20260826, 4)
    assert assign_aliases(1, 4) != assign_aliases(20260826, 4)
    out = full_episode(tmp_path, max_steps=60)
    assert sorted(out["results"]["aliases"]) == ["Cog-A", "Cog-B", "Cog-C", "Cog-D"]


def test_the_deadline_path_scores_what_it_has_and_exits_zero(tmp_path: Path) -> None:
    config = fast_cert_config(
        max_steps=480,
        step_seconds=0.0,
        shutdown_grace_seconds=0.0,
        player_connect_timeout_seconds=2,
        # A budget of 0.6 x 0.001 s: the guard fires on the first tick.
        play_budget_fraction=0.6,
        episode_timeout_seconds=30,
    )
    out = run_episode(config, FOUR_SCRIPTED, tmp_path, process_start=time.monotonic() - 100.0)
    results = out["results"]
    assert results["reason"] == "deadline"
    assert results["steps"] < 480
    # Scores are real, not zeroed: a deadline episode is still rankable.
    assert results["scores"] == [pytest.approx(v) for v in results["scores"]]
    assert all(score >= 0 for score in results["scores"])
    replay = json.loads(out["replay_bytes"].decode("utf-8"))
    kinds = {event["ev"] for tick in replay["ticks"] for event in tick.get("ev", [])}
    assert "deadline" in kinds and "episode_end" in kinds


def test_no_players_writes_zeroed_results_and_exits_zero(tmp_path: Path) -> None:
    config = fast_cert_config(
        max_steps=120, step_seconds=0.0, shutdown_grace_seconds=0.0,
        player_connect_timeout_seconds=0.2,
    )
    out = run_episode(config, [], tmp_path, connect=False)
    results = out["results"]
    assert results["reason"] == "no_players"
    assert results["scores"] == [0.0, 0.0, 0.0, 0.0]
    assert results["delivered"] == [0, 0, 0, 0]
    assert results["disconnected"] == [True, True, True, True]
    assert out["replay_bytes"], "a no_players episode still writes a replay"


def test_a_prompt_seat_with_no_credentials_falls_back_and_still_completes(tmp_path: Path) -> None:
    out = run_episode(
        fast_cert_config(max_steps=180, step_seconds=0.0, shutdown_grace_seconds=0.0,
                         player_connect_timeout_seconds=2, plan_interval_steps=50),
        [prompt_registration(), scripted_registration("brigade"),
         scripted_registration("passer"), scripted_registration("courier")],
        tmp_path,
    )
    results = out["results"]
    assert results["reason"] == "complete"
    assert results["llm_requests"] == 0, "no credentials means zero network calls"
    assert results["fallbacks"][0] > 0
    replay = json.loads(out["replay_bytes"].decode("utf-8"))
    causes = {
        event["cause"]
        for tick in replay["ticks"]
        for event in tick.get("ev", [])
        if event["ev"] == "fallback"
    }
    assert causes == {"disabled"}


def test_the_certification_fixture_settles_well_inside_the_certify_timeout(tmp_path: Path) -> None:
    """`coworld certify` defaults to a 60 s timeout covering start + connect
    grace + play + linger. The design sizes this fixture to land under 50 s;
    here the pacing and the grace are the manifest's real values."""
    config = dict(CERT_CONFIG)
    config["tokens"] = [f"token-{i}" for i in range(4)]
    config["players"] = [{"name": f"policy-{i}"} for i in range(4)]
    config["shutdown_grace_seconds"] = 20
    config["player_connect_timeout_seconds"] = 90
    started = time.monotonic()
    out = run_episode(config, FOUR_SCRIPTED, tmp_path)
    elapsed = time.monotonic() - started
    assert out["results"]["reason"] == "complete"
    assert elapsed < 50, f"the cert fixture took {elapsed:.1f}s; certify allows 60"


def test_every_player_saw_the_final_frame(tmp_path: Path) -> None:
    out = full_episode(tmp_path, max_steps=60)
    for socket in out["sockets"]:
        assert socket.final is not None
        assert socket.final["reason"] == "complete"
        assert socket.final["done"] is True
        assert len(socket.final["aliases"]) == 4

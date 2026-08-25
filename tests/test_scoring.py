"""The scoring formula, its sign, and the shape of `results.json`."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from collab_cooking.coworld.results import (
    EPSILON,
    LEGAL_REASONS,
    build_results,
    max_tickets,
    seat_scores,
)
from tests.harness import fast_cert_config, prompt_registration, run_episode, scripted_registration


def sample(**overrides) -> dict:
    base = dict(
        reason="complete",
        layout="forced",
        steps=900,
        delivered=[12, 10, 9, 6],
        served_by_recipe={"salad": 14, "soup": 15, "fries": 8},
        orders_arrived=50,
        orders_expired=13,
        burned={"pot": 2, "fryer": 1},
        blocked_moves=[41, 33, 58, 29],
        handoffs=[9, 7, 12, 4],
        names=["collab-cooking-expo", "collab-cooking-linecook", "Baseline (1)", "Baseline (2)"],
        aliases=["Cog-A", "Cog-B", "Cog-C", "Cog-D"],
        seat_kinds=["prompt", "prompt", "scripted:brigade", "scripted:passer"],
        disconnected=[False, False, False, False],
        fallbacks=[0, 1, 0, 0],
        llm_requests=68,
    )
    base.update(overrides)
    return build_results(**base)


def test_score_is_team_dishes_plus_an_epsilon_of_own_deliveries() -> None:
    delivered = [12, 10, 9, 6]
    dishes = sum(delivered)
    assert seat_scores(delivered) == [
        pytest.approx(dishes + EPSILON * value) for value in delivered
    ]
    results = sample()
    assert results["dishes"] == dishes
    assert results["scores"] == seat_scores(delivered)


def test_no_score_is_ever_negative() -> None:
    for delivered in ([0, 0, 0, 0], [1, 0, 0, 0], [50, 0, 0, 0]):
        assert all(score >= 0 for score in seat_scores(delivered))
    # Expired orders and burned pots subtract nothing.
    quiet = sample(delivered=[0, 0, 0, 0], orders_expired=50, burned={"pot": 9, "fryer": 9})
    assert quiet["scores"] == [0.0, 0.0, 0.0, 0.0]


def test_the_epsilon_can_never_reorder_two_different_team_totals() -> None:
    # 900 ticks at one ticket every 18 caps delivered[i] at 50, so the whole
    # tie-break term is bounded by 0.5 -- strictly less than one dish.
    cap = max_tickets(900)
    assert cap == 50
    assert EPSILON * cap == pytest.approx(0.5)
    assert EPSILON * cap < 1.0
    better_team = seat_scores([1, 0, 0, 0])
    worse_team_hog = seat_scores([0, 0, 0, 0])
    assert min(better_team) > max(worse_team_hog)


def test_scores_never_decrease_over_an_episode() -> None:
    out = run_episode(
        fast_cert_config(max_steps=240, step_seconds=0.0, shutdown_grace_seconds=0.0,
                         player_connect_timeout_seconds=2),
        [scripted_registration("brigade") for _ in range(4)],
        Path(tempfile.mkdtemp()),
    )
    import json

    replay = json.loads(out["replay_bytes"].decode("utf-8"))
    previous = [0, 0, 0, 0]
    for tick in replay["ticks"]:
        current = tick["sc"]
        assert all(now >= before for now, before in zip(current, previous, strict=True))
        previous = current


def test_results_shape() -> None:
    results = sample()
    for key in ("scores", "delivered", "names", "aliases", "seat_kinds", "disconnected",
                "fallbacks", "blocked_moves", "handoffs"):
        assert len(results[key]) == 4, key
    assert results["reason"] in LEGAL_REASONS
    assert set(results["served_by_recipe"]) == {"salad", "soup", "fries"}
    assert set(results["burned"]) == {"pot", "fryer"}


def test_reason_outside_the_enum_is_refused() -> None:
    with pytest.raises(ValueError, match="illegal results.reason"):
        sample(reason="crashed")


def test_cross_play_is_true_only_when_a_prompt_and_a_scripted_seat_sat_together() -> None:
    assert sample(seat_kinds=["prompt", "scripted:brigade", "scripted:passer", "prompt"])["cross_play"]
    assert not sample(seat_kinds=["prompt"] * 4)["cross_play"]
    assert not sample(seat_kinds=["scripted:brigade"] * 4)["cross_play"]


def test_a_real_episode_reports_cross_play_and_its_seat_kinds() -> None:
    out = run_episode(
        fast_cert_config(max_steps=120, step_seconds=0.0, shutdown_grace_seconds=0.0,
                         player_connect_timeout_seconds=2),
        [prompt_registration(), scripted_registration("brigade"),
         scripted_registration("passer"), scripted_registration("courier")],
        Path(tempfile.mkdtemp()),
    )
    results = out["results"]
    assert results["seat_kinds"] == [
        "prompt", "scripted:brigade", "scripted:passer", "scripted:courier"
    ]
    assert results["cross_play"] is True
    assert results["dishes"] == sum(results["delivered"])
    assert results["scores"] == seat_scores(results["delivered"])

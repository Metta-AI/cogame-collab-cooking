"""Every published variant's own `game_config` has to CONSTRUCT.

The defect this file exists for: the kitchen used to mint one resource per
prospective ticket, so the resource list -- and with it mettagrid's feature-id
space -- grew with `max_steps`. mettagrid packs a feature id into one byte
(`token_value_base = 256`) and every resource costs four ids, so at the 900
`max_steps` all eight variants declare, `GameConfig` construction raised and
`create_app()` exited 1 before uvicorn ever bound: every league episode died
`game_unhealthy / Game container exited with code 1`. The certification fixture
declares 480, fits, and certified green while the ladder was dead (2026-08-25,
cow_127a462a).

So the fixtures here are the COMMITTED manifest's own `game_config` objects,
parsed out of the template and handed to the same `mission_for_config` call
`create_app` makes, and the assertion is on the feature-id budget rather than
merely on "it did not raise".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mettagrid.policy.policy_env_interface import PolicyEnvInterface
from mettagrid.simulator import Simulator

from collab_cooking.coworld.live_episode import EpisodeConfig
from collab_cooking.missions.kitchen import mission_for_config

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "coworld_manifest_template.json").read_text(encoding="utf-8"))

# mettagrid's hard ceiling is 256 (one byte per feature id). The budget is the
# headroom this game keeps under it, so a future variant tweak that costs a few
# resources fails here instead of on the ladder.
FEATURE_ID_CEILING = 256
FEATURE_ID_BUDGET = 200

PUBLISHED_CONFIGS: list[tuple[str, dict[str, Any]]] = [
    *[(variant["id"], variant["game_config"]) for variant in MANIFEST["variants"]],
    ("certification", MANIFEST["certification"]["game_config"]),
]


def feature_ids(game_config: dict[str, Any]) -> list[int]:
    """Construct the env exactly as `create_app` does and read its features.

    `Simulator().new_simulation` is where the old encoding raised, so calling it
    IS the reproduction; `PolicyEnvInterface` then reports the ids the engine
    packed into a byte.
    """
    config = EpisodeConfig.from_mapping(game_config)
    config.num_agents = len(game_config["players"])
    sim = Simulator().new_simulation(mission_for_config(config), seed=config.seed)
    return [feature.id for feature in PolicyEnvInterface.from_mg_cfg(sim.config).obs_features]


@pytest.mark.parametrize(("name", "game_config"), PUBLISHED_CONFIGS, ids=[n for n, _ in PUBLISHED_CONFIGS])
def test_every_published_game_config_constructs_within_the_feature_budget(
    name: str, game_config: dict[str, Any]
) -> None:
    ids = feature_ids(game_config)
    assert ids, name
    assert max(ids) < FEATURE_ID_CEILING, (
        f"{name}: feature id {max(ids)} does not fit mettagrid's one-byte token value"
    )
    assert len(ids) <= FEATURE_ID_BUDGET, f"{name}: {len(ids)} feature ids leaves no headroom"


def test_the_variants_the_league_runs_are_the_length_the_design_pins() -> None:
    """The fixtures above are only evidence while they are the league's own.

    If a future change lowers `max_steps` to dodge a construction failure
    instead of fixing the encoding, this fails: the design's 900-tick episode
    is the product, not a tuning knob.
    """
    for variant in MANIFEST["variants"]:
        assert variant["game_config"]["max_steps"] == 900, variant["id"]


@pytest.mark.parametrize("max_steps", [120, 480, 900, 3600])
def test_the_feature_id_count_does_not_grow_with_max_steps(max_steps: int) -> None:
    """The invariant that keeps the defect from coming back in another form."""
    base = dict(MANIFEST["variants"][0]["game_config"])
    reference = len(feature_ids({**base, "max_steps": 480}))
    assert len(feature_ids({**base, "max_steps": max_steps})) == reference
    assert reference <= FEATURE_ID_BUDGET

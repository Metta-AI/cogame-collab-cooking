"""The one mission this coworld runs: four cogs, one kitchen.

Ported from the starter's `missions/basic.py`, which was already authored at
`num_agents=4`.
"""

from __future__ import annotations

from mettagrid.config.mettagrid_config import MettaGridConfig

from collab_cooking.game.game import KitchenSettings, make_env

NUM_AGENTS = 4


def make_kitchen_mission(
    layout: str = "open-kitchen",
    max_steps: int = 900,
    *,
    num_agents: int = NUM_AGENTS,
    **overrides: int,
) -> MettaGridConfig:
    settings = KitchenSettings(
        layout=layout, max_steps=max_steps, num_agents=num_agents, **overrides
    )
    return make_env(settings)

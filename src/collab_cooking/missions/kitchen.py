"""The one mission this coworld runs: four cogs, one kitchen.

Ported from the starter's `missions/basic.py`, which was already authored at
`num_agents=4`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mettagrid.config.mettagrid_config import MettaGridConfig

from collab_cooking.game.game import KitchenSettings, make_env

if TYPE_CHECKING:
    from collab_cooking.coworld.live_episode import EpisodeConfig

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


def mission_for_config(config: EpisodeConfig) -> MettaGridConfig:
    """The env `create_app` builds, from a resolved episode config.

    The server and the regression test that builds every published variant go
    through this one call, so a variant the server would refuse cannot pass a
    test that constructs the kitchen its own way.
    """
    return make_kitchen_mission(
        config.layout,
        config.max_steps,
        num_agents=config.num_agents,
        ticket_interarrival=config.ticket_interarrival,
        ticket_deadline=config.ticket_deadline,
        order_queue_max=config.order_queue_max,
        chop_ticks=config.chop_ticks,
        wash_ticks=config.wash_ticks,
        soup_cook_ticks=config.soup_cook_ticks,
        soup_burn_ticks=config.soup_burn_ticks,
        fries_cook_ticks=config.fries_cook_ticks,
        fries_burn_ticks=config.fries_burn_ticks,
    )

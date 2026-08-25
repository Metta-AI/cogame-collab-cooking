"""`results.json` -- what the league ranks by.

```
delivered[i]      = sim.episode_rewards[i]        # integer: dishes seat i served
dishes            = sum(delivered)                # the team score
results.scores[i] = dishes + 0.01 * delivered[i]
```

Higher is better and no term is ever negative. Expired orders and burned pots
subtract nothing -- they cost dishes, which is the only currency. The epsilon
term exists so the ladder is not a draw machine and is bounded by
`0.01 * max_tickets` (0.5 at 900 ticks), strictly less than one dish, so the
ordering is lexicographic: team dishes first, own deliveries only as a
tie-break.
"""

from __future__ import annotations

import json
from typing import Any

RESULTS_PROTOCOL = "collab-cooking.results.v1"
GAME_NAME = "collab_cooking"
EPSILON = 0.01
LEGAL_REASONS: tuple[str, ...] = ("complete", "deadline", "no_players")


def seat_scores(delivered: list[int]) -> list[float]:
    dishes = sum(delivered)
    return [round(dishes + EPSILON * value, 4) for value in delivered]


def max_tickets(max_steps: int, interarrival: int = 18) -> int:
    if interarrival <= 0:
        return 0
    return (max_steps + interarrival - 1) // interarrival


def build_results(
    *,
    reason: str,
    layout: str,
    steps: int,
    delivered: list[int],
    served_by_recipe: dict[str, int],
    orders_arrived: int,
    orders_expired: int,
    burned: dict[str, int],
    blocked_moves: list[int],
    handoffs: list[int],
    names: list[str],
    aliases: list[str],
    seat_kinds: list[str],
    disconnected: list[bool],
    fallbacks: list[int],
    llm_requests: int,
) -> dict[str, Any]:
    if reason not in LEGAL_REASONS:
        raise ValueError(f"illegal results.reason {reason!r}; expected one of {LEGAL_REASONS}")
    dishes = sum(delivered)
    prompt_seated = any(kind == "prompt" for kind in seat_kinds)
    scripted_seated = any(kind.startswith("scripted") for kind in seat_kinds)
    return {
        "game": GAME_NAME,
        "protocol": RESULTS_PROTOCOL,
        "reason": reason,
        "layout": layout,
        "steps": int(steps),
        "dishes": int(dishes),
        "scores": seat_scores(delivered),
        "delivered": [int(v) for v in delivered],
        "served_by_recipe": {
            "salad": int(served_by_recipe.get("salad", 0)),
            "soup": int(served_by_recipe.get("soup", 0)),
            "fries": int(served_by_recipe.get("fries", 0)),
        },
        "orders_arrived": int(orders_arrived),
        "orders_expired": int(orders_expired),
        "burned": {"pot": int(burned.get("pot", 0)), "fryer": int(burned.get("fryer", 0))},
        "blocked_moves": [int(v) for v in blocked_moves],
        "handoffs": [int(v) for v in handoffs],
        "names": list(names),
        "aliases": list(aliases),
        "seat_kinds": list(seat_kinds),
        "cross_play": bool(prompt_seated and scripted_seated),
        "disconnected": [bool(v) for v in disconnected],
        "fallbacks": [int(v) for v in fallbacks],
        "llm_requests": int(llm_requests),
    }


def encode(results: dict[str, Any]) -> bytes:
    """UTF-8 exactly once, `ensure_ascii=False`."""
    return json.dumps(results, ensure_ascii=False).encode("utf-8")

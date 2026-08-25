"""Generate coworld_manifest_template.json.

The manifest is long and every array in it is a cross-check on another
(`num_agents` appears in eight variants, the certification fixture, the config
schema and `SMOKE_SEATS`), so it is generated rather than hand-maintained and
the generator is committed. `tests/test_manifest.py` asserts the invariants on
the committed file, not on this script's output, so an edit by hand is still
checked.

    python3 tools/build_manifest.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from collab_cooking.kitchens.layouts import (  # noqa: E402
    LAYOUT_BLURBS,
    LAYOUT_NAMES,
)

IMAGE = "{{COLLAB_COOKING_IMAGE}}"
GAME_NAME = "collab_cooking"
OWNER = "daveey@softmax.com"
SLUG = "collab-cooking"
NUM_AGENTS = 4
MAX_STEPS = 900

VARIANT_TITLES = {
    "open-kitchen": "Open Kitchen",
    "cramped": "Cramped Room",
    "forced": "Forced Coordination",
    "crowded": "Crowded",
    "asymmetric": "Asymmetric Advantages",
    "circuit": "Counter Circuit",
    "ring": "Ring",
    "figure-eight": "Figure Eight",
}
VARIANT_SEEDS = {name: 20260825 + index for index, name in enumerate(LAYOUT_NAMES)}

PLAYER_NAMES = ["Cog One", "Cog Two", "Cog Three", "Cog Four"]

REFERENCE_PROMPT = (
    "Work one job at a time and say which job you have taken so nobody duplicates it. "
    "Look at the board early, name the recipe you are working, and keep the pot and the "
    "fryer busy. If your item's next station is across a counter, put it on the counter "
    "and say so rather than walking round."
)

BUNDLED_PLAYERS = [
    {
        "id": "collab-prompt",
        "name": "Reference prompt",
        "description": "The reference prompt policy: an LLM shift order every 50 ticks.",
        "env": {"PLAYER_PROMPT": REFERENCE_PROMPT},
    },
    {
        "id": "brigade",
        "name": "Brigade",
        "description": "prep / cook / server / all-rounder roles, the shipped scripted brain.",
        "env": {"PLAYER_SCRIPTED": "brigade"},
    },
    {
        "id": "passer",
        "name": "Passer",
        "description": "never crosses the midline; always stages on the pass counter.",
        "env": {"PLAYER_SCRIPTED": "passer"},
    },
    {
        "id": "courier",
        "name": "Courier",
        "description": "every seat serves; the greedy-service control.",
        "env": {"PLAYER_SCRIPTED": "courier"},
    },
]

CERT_GAME_CONFIG = {
    "num_agents": NUM_AGENTS,
    "layout": "cramped",
    "max_steps": 480,
    "step_seconds": 0.02,
    "policy_action_timeout_seconds": 0.30,
    "plan_interval_steps": 240,
    "player_connect_timeout_seconds": 90,
    "seed": 20260826,
    "players": [{"name": name} for name in PLAYER_NAMES],
    "tokens": ["token-0", "token-1", "token-2", "token-3"],
}


def string_array(minimum: int, maximum: int, description: str) -> dict:
    return {
        "type": "array",
        "description": description,
        "items": {"type": "string"},
        "minItems": minimum,
        "maxItems": maximum,
    }


def config_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["tokens", "players", "num_agents"],
        "properties": {
            "tokens": string_array(NUM_AGENTS, NUM_AGENTS, "One connect token per seat."),
            "players": {
                "type": "array",
                "description": "Real policy names, spectator-side only.",
                "minItems": NUM_AGENTS,
                "maxItems": NUM_AGENTS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                },
            },
            "num_agents": {"type": "integer", "minimum": NUM_AGENTS, "maximum": NUM_AGENTS},
            "layout": {"type": "string", "enum": list(LAYOUT_NAMES)},
            "seed": {"type": "integer", "minimum": 0},
            "max_steps": {"type": "integer", "minimum": 1, "maximum": 2000},
            "step_seconds": {"type": "number", "minimum": 0.01, "maximum": 1.0},
            "policy_action_timeout_seconds": {"type": "number", "minimum": 0.01, "maximum": 5.0},
            "player_connect_timeout_seconds": {"type": "number", "minimum": 1, "maximum": 600},
            "plan_interval_steps": {"type": "integer", "minimum": 1, "maximum": 1000},
            "min_plan_interval_seconds": {"type": "number", "minimum": 0, "maximum": 120},
            "plan_timeout_seconds": {"type": "number", "minimum": 1, "maximum": 60},
            "llm_max_requests_per_minute": {"type": "integer", "minimum": 1, "maximum": 120},
            "fallback_scripted": {
                "type": "string",
                "enum": ["brigade", "runner", "passer", "courier"],
            },
            "play_budget_fraction": {"type": "number", "minimum": 0.05, "maximum": 1.0},
            "episode_timeout_seconds": {"type": "number", "minimum": 30, "maximum": 3600},
            "shutdown_grace_seconds": {"type": "number", "minimum": 0, "maximum": 120},
            "ticket_interarrival": {"type": "integer", "minimum": 1, "maximum": 200},
            "ticket_deadline": {"type": "integer", "minimum": 1, "maximum": 500},
            "order_queue_max": {"type": "integer", "minimum": 1, "maximum": 32},
            "chop_ticks": {"type": "integer", "minimum": 2, "maximum": 20},
            "wash_ticks": {"type": "integer", "minimum": 2, "maximum": 20},
            "soup_cook_ticks": {"type": "integer", "minimum": 1, "maximum": 100},
            "soup_burn_ticks": {"type": "integer", "minimum": 1, "maximum": 100},
            "fries_cook_ticks": {"type": "integer", "minimum": 1, "maximum": 100},
            "fries_burn_ticks": {"type": "integer", "minimum": 1, "maximum": 100},
            "model": {"type": "string"},
            "max_output_tokens": {"type": "integer", "minimum": 64, "maximum": 4096},
        },
    }


def bounded_int_array(description: str) -> dict:
    return {
        "type": "array",
        "description": description,
        "items": {"type": "integer"},
        "minItems": NUM_AGENTS,
        "maxItems": NUM_AGENTS,
    }


def results_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "game", "protocol", "reason", "layout", "steps", "dishes", "scores",
            "delivered", "served_by_recipe", "orders_arrived", "orders_expired",
            "burned", "blocked_moves", "handoffs", "names", "aliases",
            "seat_kinds", "cross_play", "disconnected", "fallbacks", "llm_requests",
        ],
        "properties": {
            "game": {"type": "string", "const": GAME_NAME},
            "protocol": {"type": "string", "const": "collab-cooking.results.v1"},
            "reason": {"type": "string", "enum": ["complete", "deadline", "no_players"]},
            "layout": {"type": "string", "enum": list(LAYOUT_NAMES)},
            "steps": {"type": "integer", "minimum": 0},
            "dishes": {"type": "integer", "minimum": 0},
            "scores": {
                "type": "array",
                "description": "dishes + 0.01 * delivered[i]; higher is better.",
                "items": {"type": "number"},
                "minItems": NUM_AGENTS,
                "maxItems": NUM_AGENTS,
            },
            "delivered": bounded_int_array("Dishes this seat carried to the pass."),
            "served_by_recipe": {
                "type": "object",
                "additionalProperties": False,
                "required": ["salad", "soup", "fries"],
                "properties": {
                    "salad": {"type": "integer", "minimum": 0},
                    "soup": {"type": "integer", "minimum": 0},
                    "fries": {"type": "integer", "minimum": 0},
                },
            },
            "orders_arrived": {"type": "integer", "minimum": 0},
            "orders_expired": {"type": "integer", "minimum": 0},
            "burned": {
                "type": "object",
                "additionalProperties": False,
                "required": ["pot", "fryer"],
                "properties": {
                    "pot": {"type": "integer", "minimum": 0},
                    "fryer": {"type": "integer", "minimum": 0},
                },
            },
            "blocked_moves": bounded_int_array("Failed moves per seat."),
            "handoffs": bounded_int_array("Items staged on a pass counter per seat."),
            "names": string_array(NUM_AGENTS, NUM_AGENTS, "Real policy names."),
            "aliases": string_array(NUM_AGENTS, NUM_AGENTS, "In-game cog aliases."),
            "seat_kinds": string_array(NUM_AGENTS, NUM_AGENTS, "prompt | scripted:<baseline>"),
            "cross_play": {"type": "boolean"},
            "disconnected": {
                "type": "array",
                "items": {"type": "boolean"},
                "minItems": NUM_AGENTS,
                "maxItems": NUM_AGENTS,
            },
            "fallbacks": bounded_int_array("Plan turns this seat fell back to its baseline."),
            "llm_requests": {"type": "integer", "minimum": 0},
        },
    }


def variants() -> list[dict]:
    out = []
    for name in LAYOUT_NAMES:
        out.append(
            {
                "id": name,
                "name": VARIANT_TITLES[name],
                "description": f"{VARIANT_TITLES[name]} - {LAYOUT_BLURBS[name]}.",
                "game_config": {
                    "layout": name,
                    "num_agents": NUM_AGENTS,
                    "seed": VARIANT_SEEDS[name],
                    "max_steps": MAX_STEPS,
                    "step_seconds": 0.20,
                    "plan_interval_steps": 50,
                    "player_connect_timeout_seconds": 120,
                    "players": [{"name": player} for player in PLAYER_NAMES],
                },
            }
        )
    return out


def docs() -> dict:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    pages = []
    for page_id, title in (
        ("rules", "Rules"),
        ("kitchens", "The eight kitchens"),
        ("policies", "Policies"),
        ("protocol", "Protocol"),
    ):
        body = (ROOT / "docs" / f"{page_id}.md").read_text(encoding="utf-8")
        pages.append(
            {"id": page_id, "title": title, "content": {"type": "text", "value": body}}
        )
    return {"readme": {"type": "text", "value": readme}, "pages": pages}


def protocols() -> dict:
    player = (ROOT / "docs" / "protocol.md").read_text(encoding="utf-8")
    return {
        "player": {"type": "text", "value": player},
        "global": {
            "type": "text",
            "value": (ROOT / "docs" / "protocol_global.md").read_text(encoding="utf-8"),
        },
    }


def manifest() -> dict:
    return {
        "$schema": "https://softmax.com/schemas/coworld-manifest.json",
        "tags": ["cooperation", "melting-pot", "grid", "kitchen", "multi-agent", "llm"],
        "episode_timeout_minutes": 20,
        "game": {
            "name": GAME_NAME,
            # `coworld build` validates this file with its own loader before it
            # touches docker, and the top level is `extra="forbid"`: the ONLY
            # place `replay_viewer` is read from is `game` (bundle.py:81,
            # upload.py:927), `owner` is required, and neither a top-level
            # `version` nor a `game.display_name` exists in the schema.
            # `game.version` is set by `coworld build --version`, so the file
            # must not carry it either.
            "owner": OWNER,
            "replay_viewer": {"bundle": "static-replay-viewer"},
            "description": (
                "Four cogs share one kitchen for 900 ticks. Tickets arrive on an order "
                "board and expire; a dish is a chain of single-item errands and a cog "
                "can carry exactly one thing. Team score = dishes served. Eight Melting "
                "Pot kitchens, each isolating one coordination problem."
            ),
            "runnable": {
                "type": "game",
                "image": IMAGE,
                "run": ["/bin/collab-cooking"],
                "env": {
                    # The namespace is game.name, NOT the slug -- they differ here
                    # (collab_cooking vs collab-cooking). Without this the hosted
                    # game container never receives the secret and every league
                    # episode silently plays scripted.
                    "ANTHROPIC_API_KEY_URI": f"secret://coworld/{GAME_NAME}/anthropic_api_key"
                },
            },
            "config_schema": config_schema(),
            "results_schema": results_schema(),
            "docs": docs(),
            "protocols": protocols(),
        },
        "variants": variants(),
        "player": [
            {
                "id": entry["id"],
                "type": "player",
                "name": entry["name"],
                "description": entry["description"],
                "image": IMAGE,
                "run": ["/bin/collab-cooking-player"],
                "env": entry["env"],
            }
            for entry in BUNDLED_PLAYERS
        ],
        "certification": {
            "game_config": CERT_GAME_CONFIG,
            # Every declared bundled player occupies a slot: a fixture that omits
            # one fails `players_missing`. One prompt seat plus three scripted
            # partners makes the fixture cross-play by construction.
            "players": [
                {"player_id": "collab-prompt"},
                {"player_id": "brigade"},
                {"player_id": "passer"},
                {"player_id": "courier"},
            ],
        },
    }


def main() -> None:
    out = ROOT / "coworld_manifest_template.json"
    out.write_text(json.dumps(manifest(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

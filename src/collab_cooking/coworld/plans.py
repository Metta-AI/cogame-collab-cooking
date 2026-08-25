"""The plan reply schema, its caps, and rune-boundary truncation.

One JSON object per seat per plan turn. Unknown keys are ignored; extraction
takes the first balanced ``{...}`` span, so leading or trailing prose is
tolerated.

Every free-text field is truncated on **rune** boundaries, never byte
boundaries -- a byte-cut multi-byte rune is exactly what makes replay bytes
fail a strict JSON parser while still rendering in a browser. In Python a
``str`` slice is already a code-point slice, so the truncator is this one
helper, applied to ``say``, ``note``, the registered prompt, policy names, the
engine ``talk`` string and every error string that can reach the replay.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from collab_cooking.agent.brain.policy import PlanDirective

# Field caps, exactly as the design note's table states them.
STATION_CAP = 12
RECIPE_CAP = 6
ZONE_CAP = 6
ALIAS_CAP = 8
SAY_RUNES = 120
NOTE_RUNES = 200
PROMPT_RUNES = 1200
POLICY_NAME_RUNES = 48
TALK_RUNES = 140
ERROR_RUNES = 240

RECIPES: tuple[str, ...] = ("salad", "soup", "fries", "any")
ZONES: tuple[str, ...] = ("left", "right", "pass", "any")

FALLBACK_CAUSES: tuple[str, ...] = (
    "timeout",
    "parse",
    "illegal_station",
    "rate_budget",
    "transport",
    "disabled",
)

RETRY_HINT = (
    "Your last reply was not usable. Reply with ONE JSON object beginning with "
    "{ and a station from LEGAL STATIONS."
)


def truncate_runes(text: Any, cap: int) -> str:
    """Cut `text` to at most `cap` code points. Never splits a rune."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if len(text) <= cap:
        return text
    return text[:cap]


class PlanError(Exception):
    """A reply that cannot become a directive. Carries its fallback cause."""

    def __init__(self, cause: str, detail: str = "") -> None:
        super().__init__(f"{cause}: {detail}" if detail else cause)
        self.cause = cause if cause in FALLBACK_CAUSES else "parse"
        self.detail = truncate_runes(detail, ERROR_RUNES)


@dataclass(frozen=True, slots=True)
class ParsedPlan:
    """A validated reply, ready to become a `PlanDirective`.

    `note` is private: it is echoed back only to its own seat and is **never**
    written to the replay.
    """

    station: str
    recipe: str
    zone: str
    handoff: str
    yield_to: str
    say: str
    note: str

    def directive(self, turn: int, src: str) -> PlanDirective:
        return PlanDirective(
            turn=turn,
            station=self.station,
            recipe=self.recipe,
            zone=self.zone,
            handoff=self.handoff,
            yield_to=self.yield_to,
            say=self.say,
            src=src,
        )

    def replay_event(self, slot: int, alias: str, turn: int, src: str) -> dict[str, Any]:
        """The `plan` event. Carries no `note` -- that is the point."""
        return {
            "ev": "plan",
            "slot": slot,
            "alias": alias,
            "turn": turn,
            "station": self.station,
            "recipe": self.recipe,
            "zone": self.zone,
            "handoff": self.handoff,
            "yield_to": self.yield_to,
            "say": self.say,
            "src": src,
        }


def extract_object(text: str) -> str:
    """The first balanced `{...}` span in `text`.

    Prose before or after the object is tolerated; a brace inside a JSON string
    literal does not count toward the balance.
    """
    if not isinstance(text, str):
        raise PlanError("parse", "reply was not text")
    start = text.find("{")
    if start < 0:
        raise PlanError("parse", "no JSON object in the reply")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise PlanError("parse", "unbalanced JSON object in the reply")


def parse_plan(text: str, legal_stations: list[str], ally_aliases: list[str]) -> ParsedPlan:
    """Validate one reply against `LEGAL STATIONS` and the caps.

    `station` outside the legal set is the one **illegal** outcome; every other
    field degrades to its default rather than failing the reply.
    """
    span = extract_object(text)
    try:
        raw = json.loads(span)
    except Exception as exc:  # noqa: BLE001 - any decode failure is one cause
        raise PlanError("parse", f"json: {exc}") from exc
    if not isinstance(raw, dict):
        raise PlanError("parse", "reply was not a JSON object")

    station = truncate_runes(raw.get("station"), STATION_CAP).lower()
    if station not in legal_stations:
        raise PlanError("illegal_station", f"station={station or '(missing)'}")

    recipe = truncate_runes(raw.get("recipe"), RECIPE_CAP).lower()
    if recipe not in RECIPES:
        recipe = "any"
    zone = truncate_runes(raw.get("zone"), ZONE_CAP).lower()
    if zone not in ZONES:
        zone = "any"
    handoff = truncate_runes(raw.get("handoff"), ALIAS_CAP)
    if handoff not in ally_aliases:
        handoff = "none"
    yield_to = truncate_runes(raw.get("yield_to"), ALIAS_CAP)
    if yield_to not in ally_aliases:
        yield_to = "none"

    return ParsedPlan(
        station=station,
        recipe=recipe,
        zone=zone,
        handoff=handoff,
        yield_to=yield_to,
        say=truncate_runes(raw.get("say"), SAY_RUNES),
        note=truncate_runes(raw.get("note"), NOTE_RUNES),
    )

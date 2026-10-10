"""Exact source-owned private prompt renderer for native container players."""

from typing import Any

SYSTEM_TEMPLATE = """You are {alias}, one of 4 cogs running a kitchen together for {max_steps} ticks.
Orders arrive on the board every 18 ticks and expire 50 ticks later. A dish is a chain:
fetch veg or meat -> chop it (3 uses) -> pot it (soup: chopped veg + chopped meat, 10 ticks,
burns 14 ticks after it is ready) or fry it (chopped veg, 8 ticks, burns after 11) or plate
chopped veg as a salad -> carry it to the pass and serve it against a live ticket -> the plate
comes back dirty and needs 3 uses at the sink.
YOU CAN CARRY EXACTLY ONE THING. Counters (the walls) hold one item each: put your item down
and a team-mate can pick it up. That is often faster than walking round.
Your score and everyone else's is the same number: dishes the team serves. Nothing else scores.
Kitchen: {layout} - {layout_line}
You give one standing order; a controller walks you there tick by tick until your next order,
about {plan_interval} ticks from now.
Reply with a single JSON object and NOTHING else. Your reply MUST begin with the character {{.
Schema:
{{"station":"<one of LEGAL STATIONS>","recipe":"salad|soup|fries|any","zone":"left|right|pass|any",
 "handoff":"<ally alias or none>","yield_to":"<ally alias or none>",
 "say":"<=120 chars","note":"<=200 chars"}}
"say" is heard by your team-mates next turn and shown to spectators. "note" is private and is
handed back only to you."""

USER_CAP = 2000


def build_system_prompt(
    alias: str, max_steps: int, layout: str, layout_line: str, plan_interval: int, standing_orders: str
) -> str:
    """The constant system block, with the seat's PLAYER_PROMPT appended."""
    body = SYSTEM_TEMPLATE.format(
        alias=alias,
        max_steps=max_steps,
        layout=layout,
        layout_line=layout_line,
        plan_interval=plan_interval,
    )
    if standing_orders:
        body = f"{body}\nSTANDING ORDERS: {standing_orders}"
    return body


def _fmt_pos(pos: tuple[int, int]) -> str:
    return f"({pos[0]},{pos[1]})"


def build_user_message(view: dict[str, Any]) -> str:
    """The per-turn observation. Deterministic, every list bounded, <= 2000 chars."""
    lines: list[str] = []
    lines.append(
        f"TURN {view['turn']}/{view['turns']}  TICK {view['tick']}/{view['ticks']}  "
        f"KITCHEN {view['layout']}  DISHES {view['dishes']}  TICKETS LIVE {view['live']}"
    )
    lines.append(
        f"YOU {view['alias']} at {_fmt_pos(view['position'])} carrying "
        f"{view['carrying'] or 'nothing'}  served {view['delivered']}"
    )
    lines.append("TEAM (last seen)")
    for mate in view["team"][:3]:
        if mate["seen"] is None:
            lines.append(f"  {mate['alias']} not seen yet")
        elif mate["pos"] is None:
            lines.append(f"  {mate['alias']} not seen since tick {mate['seen']}")
        else:
            lines.append(
                f"  {mate['alias']} {_fmt_pos(mate['pos'])} {mate['age']} ticks ago "
                f"carrying {mate['carrying'] or 'nothing'}"
            )
    board = view["board"]
    if board["age"] is None:
        lines.append("BOARD: not seen yet - go and look at it")
    else:
        lines.append(
            f"BOARD (seen {board['age']} ticks ago): soup {board['soup']}, "
            f"salad {board['salad']}, fries {board['fries']}"
        )
    lines.append("STATIONS YOU KNOW")
    for station in view["stations"][:9]:
        note = f": {station['note']}" if station["note"] else ""
        lines.append(f"  {station['name']} {_fmt_pos(station['pos'])} {station['age']} ticks ago{note}")
    held = view["counters"][:6]
    if held:
        lines.append("COUNTERS HOLDING SOMETHING (<=6): " + ", ".join(f"{_fmt_pos(pos)} {item}" for pos, item in held))
    lines.append(
        f"BLOCKED LAST TURN: {view['blocked']} times"
        + (f", mostly at {_fmt_pos(view['blocked_at'])}" if view["blocked_at"] else "")
    )
    lines.append("LEGAL STATIONS: " + ", ".join(view["legal_stations"]))
    lines.append(f"LAST ORDER: {view['last_order']}")
    radio = view["radio"][:3]
    if radio:
        lines.append("TEAM RADIO (<=3 lines)")
        for who, what in radio:
            lines.append(f"  {who}: {what}")
    if view["note"]:
        lines.append(f"YOUR NOTE: {view['note']}")
    text = "\n".join(lines)
    return text if len(text) <= USER_CAP else text[:USER_CAP]

"""A strict UTF-8 parse of the replay an episode actually wrote.

`data.decode("utf-8")` with NO error handler, then `json.loads`. A string
truncated on a byte boundary mid-rune renders fine in a browser and fails
exactly here, which is why every recorded string is cut on rune boundaries.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from collab_cooking.coworld.llm import LlmPlanner
from collab_cooking.coworld.plans import SAY_RUNES
from collab_cooking.coworld.replay import (
    DIFF_ORDER,
    EVENT_NAMES,
    REPLAY_FORMAT,
    REPLAY_PROTOCOL,
)
from collab_cooking.game.game import (
    TICKET_DEADLINE,
    TICKET_FIRST_ARRIVAL,
    TICKET_INTERARRIVAL,
)
from tests.harness import fast_cert_config, prompt_registration, run_episode, scripted_registration
from tests.test_llm import StubTransport

REQUIRED_KEYS = ["format", "protocol", "config", "seed", "kitchen", "seats", "ticks", "heat", "results"]
# A say at exactly the cap, every rune multi-byte: a byte cut anywhere in it
# produces a lone continuation byte and this test fails.
MULTIBYTE_SAY = "\u2603\u00e9\u4e2d" * 60  # 180 runes -> truncated to 120


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory) -> dict:
    out_dir = tmp_path_factory.mktemp("replay")
    reply = json.dumps(
        {
            "station": "chop",
            "recipe": "soup",
            "zone": "left",
            "handoff": "Cog-B",
            "yield_to": "none",
            "say": MULTIBYTE_SAY,
            "note": "\u2603" * 400,
        },
        ensure_ascii=False,
    )
    planner = LlmPlanner(transport=StubTransport([reply]), max_workers=2, timeout_seconds=5)
    out = run_episode(
        fast_cert_config(
            max_steps=200, step_seconds=0.0, shutdown_grace_seconds=0.0,
            player_connect_timeout_seconds=2, plan_interval_steps=40,
            min_plan_interval_seconds=0.0,
        ),
        [
            prompt_registration("Keep the pot busy."),
            prompt_registration("Feed the board."),
            scripted_registration("passer"),
            scripted_registration("courier"),
        ],
        Path(out_dir),
        planner=planner,
        player_names=["collab-cooking-expo", "collab-cooking-linecook", "Baseline (1)", "Baseline (2)"],
    )
    planner.shutdown()
    return out


def test_the_replay_bytes_are_strict_utf8_json(artifacts: dict) -> None:
    text = artifacts["replay_bytes"].decode("utf-8")  # no errors= handler
    document = json.loads(text)
    for key in REQUIRED_KEYS:
        assert key in document, key
    assert document["format"] == REPLAY_FORMAT
    assert document["protocol"] == REPLAY_PROTOCOL


def test_seats_carry_both_an_alias_and_a_real_name(artifacts: dict) -> None:
    document = json.loads(artifacts["replay_bytes"].decode("utf-8"))
    seats = document["seats"]
    assert len(seats) == 4
    for seat in seats:
        assert seat["alias"].startswith("Cog-")
        assert seat["name"], "the real policy name is spectator-side data and must be present"
    assert sorted(seat["alias"] for seat in seats) == ["Cog-A", "Cog-B", "Cog-C", "Cog-D"]
    assert {seat["name"] for seat in seats} == {
        "collab-cooking-expo", "collab-cooking-linecook", "Baseline (1)", "Baseline (2)"
    }


def test_every_event_is_inside_the_documented_vocabulary(artifacts: dict) -> None:
    document = json.loads(artifacts["replay_bytes"].decode("utf-8"))
    seen = {
        event["ev"]
        for tick in document["ticks"]
        for event in tick.get("ev", [])
    }
    assert seen, "an episode with no events at all is a bug"
    assert seen <= set(EVENT_NAMES), f"undocumented events: {seen - set(EVENT_NAMES)}"
    assert "plan" in seen, "the stubbed transport must have delivered plans"


def test_a_capped_multibyte_say_survives_as_valid_utf8(artifacts: dict) -> None:
    document = json.loads(artifacts["replay_bytes"].decode("utf-8"))
    says = [
        event["say"]
        for tick in document["ticks"]
        for event in tick.get("ev", [])
        if event["ev"] == "plan"
    ]
    assert says, "no plan carried a say"
    for say in says:
        assert len(say) == SAY_RUNES
        assert say == MULTIBYTE_SAY[:SAY_RUNES]
        assert say.encode("utf-8").decode("utf-8") == say
        assert all(ord(rune) > 127 for rune in say)


def test_note_is_absent_from_the_replay_entirely(artifacts: dict) -> None:
    text = artifacts["replay_bytes"].decode("utf-8")
    document = json.loads(text)
    for tick in document["ticks"]:
        for event in tick.get("ev", []):
            assert "note" not in event, "a private note must never reach the replay"
    assert "\u2603" * 200 not in text


def test_the_tick_records_have_the_documented_shape(artifacts: dict) -> None:
    document = json.loads(artifacts["replay_bytes"].decode("utf-8"))
    seen_st = 0
    for tick in document["ticks"]:
        assert isinstance(tick["t"], int)
        assert len(tick["c"]) == 4, "`c` is present on EVERY tick, four entries in slot order"
        for entry in tick["c"]:
            assert len(entry) == 5
            x, y, carrying, action, flags = entry
            assert isinstance(x, int) and isinstance(y, int)
            assert isinstance(carrying, str) and isinstance(action, str)
            assert isinstance(flags, int) and 0 <= flags <= 7
        assert len(tick["sc"]) == 4
        if "st" in tick:
            seen_st += 1
    # `st` is omitted when unchanged, so it must NOT be on every tick.
    assert 0 < seen_st < len(document["ticks"])


def test_every_tick_carries_its_events_in_the_declared_order(artifacts: dict) -> None:
    """`DIFF_ORDER` is the specification, not a comment: the viewer's ticker,
    feed and dish numbering read the list in the order it arrives."""
    document = json.loads(artifacts["replay_bytes"].decode("utf-8"))
    rank = {name: index for index, name in enumerate(DIFF_ORDER)}
    multi = 0
    for tick in document["ticks"]:
        derived = [rank[e["ev"]] for e in tick.get("ev", []) if e["ev"] in rank]
        assert derived == sorted(derived), f"tick {tick['t']} is out of DIFF_ORDER"
        slots = [
            e.get("slot")
            for e in tick.get("ev", [])
            if e["ev"] == "blocked" and e.get("slot") is not None
        ]
        assert slots == sorted(slots), f"tick {tick['t']}: ties resolve by ascending slot"
        multi += len(derived) > 1
    assert multi > 5, "the fixture must carry ticks with several events"


def test_every_live_ticket_carries_the_tick_it_expires_on(artifacts: dict) -> None:
    """The clock reads `3 ORDERS LIVE - 1 EXPIRING`, and the viewer counts a
    ticket as expiring from `expires - tick <= 12`. Without the field that
    readout can never fire."""
    document = json.loads(artifacts["replay_bytes"].decode("utf-8"))
    max_steps = document["config"]["max_steps"]
    stations: dict | None = None
    seen = 0
    expiring = 0
    for tick in document["ticks"]:
        stations = tick.get("st", stations)
        assert stations is not None
        for ticket in stations["board"]["tickets"]:
            seen += 1
            arrival = TICKET_FIRST_ARRIVAL + ticket["i"] * TICKET_INTERARRIVAL
            assert ticket["expires"] == min(max_steps, arrival + TICKET_DEADLINE)
            assert ticket["expires"] >= tick["t"], "a live ticket has not expired yet"
            if ticket["expires"] - tick["t"] <= 12:
                expiring += 1
    assert seen, "the fixture must carry live tickets"
    assert expiring, "no frame could ever have shown an EXPIRING order"


def test_the_kitchen_block_is_self_sufficient(artifacts: dict) -> None:
    kitchen = json.loads(artifacts["replay_bytes"].decode("utf-8"))["kitchen"]
    assert kitchen["w"] == len(kitchen["rows"][0])
    assert kitchen["h"] == len(kitchen["rows"])
    assert kitchen["tile"] == 24
    kinds = {station["kind"] for station in kitchen["stations"]}
    assert len(kinds) == 9
    for station in kitchen["stations"]:
        assert 0 <= station["x"] < kitchen["w"]
        assert 0 <= station["y"] < kitchen["h"]


def test_the_results_block_is_the_results_file(artifacts: dict) -> None:
    document = json.loads(artifacts["replay_bytes"].decode("utf-8"))
    assert document["results"] == artifacts["results"]
    assert document["results"]["cross_play"] is True


def test_heat_is_the_cumulative_blocked_move_count(artifacts: dict) -> None:
    document = json.loads(artifacts["replay_bytes"].decode("utf-8"))
    blocked: dict[tuple[int, int], int] = {}
    for tick in document["ticks"]:
        for event in tick.get("ev", []):
            if event["ev"] != "blocked":
                continue
            blocked[(event["x"], event["y"])] = blocked.get((event["x"], event["y"]), 0) + 1
    # Tile for tile, not just in total: the viewer accumulates the overlay live
    # from these events as the playhead moves, so a `heat` array keyed by any
    # other tile would tint different tiles from the ones the replay names.
    assert {(x, y): count for x, y, count in document["heat"]} == blocked
    assert sum(blocked.values()) > 0, "the fixture must actually block some moves"

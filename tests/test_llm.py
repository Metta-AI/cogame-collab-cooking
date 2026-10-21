"""Source-owned ordinary parser and exact private prompt rendering."""

import pytest

from collab_cooking.coworld.llm import build_system_prompt, build_user_message
from collab_cooking.coworld.plans import NOTE_RUNES, SAY_RUNES, PlanError, extract_object, parse_plan, truncate_runes
from collab_cooking.coworld.prompts import PromptSelection

LEGAL = ["veg", "meat", "plate", "chop", "sink", "board", "hold"]
ALLIES = ["Cog-A", "Cog-B", "Cog-C", "Cog-D", "none"]


# -- parsing ---------------------------------------------------------------
def test_clean_json_parses() -> None:
    plan = parse_plan(
        '{"station":"chop","recipe":"soup","zone":"left","handoff":"Cog-D",'
        '"yield_to":"none","say":"on it","note":"private"}',
        LEGAL,
        ALLIES,
    )
    assert (plan.station, plan.recipe, plan.zone) == ("chop", "soup", "left")
    assert plan.handoff == "Cog-D" and plan.yield_to == "none"
    assert plan.say == "on it" and plan.note == "private"


def test_trailing_and_leading_prose_is_tolerated() -> None:
    assert parse_plan('Sure! {"station":"sink"} hope that helps', LEGAL, ALLIES).station == "sink"
    assert parse_plan('{"station":"board"}\n\nLet me know.', LEGAL, ALLIES).station == "board"
    # A brace inside a string literal does not end the object.
    assert parse_plan('{"station":"veg","say":"} not the end"}', LEGAL, ALLIES).say == "} not the end"


def test_missing_fields_default_rather_than_failing() -> None:
    plan = parse_plan('{"station":"veg"}', LEGAL, ALLIES)
    assert (plan.recipe, plan.zone, plan.handoff, plan.yield_to) == ("any", "any", "none", "none")
    assert plan.say == "" and plan.note == ""
    junk = parse_plan(
        '{"station":"veg","recipe":"pizza","zone":"middle","handoff":"Cog-Z","yield_to":7}',
        LEGAL,
        ALLIES,
    )
    assert (junk.recipe, junk.zone, junk.handoff, junk.yield_to) == ("any", "any", "none", "none")


def test_a_station_outside_the_legal_set_is_illegal() -> None:
    with pytest.raises(PlanError) as error:
        parse_plan('{"station":"pot"}', LEGAL, ALLIES)
    assert error.value.cause == "illegal_station"


def test_unparseable_replies_are_a_parse_error() -> None:
    for text in ("no braces here", "{unterminated", '["an array"]'):
        with pytest.raises(PlanError) as error:
            parse_plan(text, LEGAL, ALLIES)
        assert error.value.cause == "parse"
    with pytest.raises(PlanError):
        extract_object("nothing")


def test_free_text_is_truncated_on_rune_boundaries() -> None:
    say = "\u00e9\u00e8\u00ea" * 200
    note = "\u2603" * 500
    plan = parse_plan(
        '{"station":"hold","say":' + f'"{say}"' + ',"note":' + f'"{note}"' + "}",
        LEGAL,
        ALLIES,
    )
    assert len(plan.say) == SAY_RUNES
    assert len(plan.note) == NOTE_RUNES
    # Rune boundaries, not byte boundaries: both round-trip through UTF-8.
    assert plan.say.encode("utf-8").decode("utf-8") == plan.say
    assert plan.note.encode("utf-8").decode("utf-8") == plan.note
    assert truncate_runes("\u2603" * 10, 3) == "\u2603\u2603\u2603"


# -- the prompt ------------------------------------------------------------
def test_the_system_prompt_demands_a_reply_that_begins_with_a_brace() -> None:
    prompt = build_system_prompt(
        "Cog-C", 900, "forced", "two sealed halves", 50, "keep the pot busy", selection=PromptSelection()
    )
    assert "MUST begin with the character {" in prompt
    assert "Cog-C" in prompt and "forced" in prompt
    assert "STANDING ORDERS: keep the pot busy" in prompt
    assert "effort" not in prompt


def test_the_user_message_is_bounded_and_carries_the_legal_set() -> None:
    view = {
        "turn": 7,
        "turns": 18,
        "tick": 350,
        "ticks": 900,
        "layout": "forced",
        "dishes": 9,
        "live": 3,
        "alias": "Cog-C",
        "position": (5, 3),
        "carrying": "chopped_veg",
        "delivered": 2,
        "team": [
            {"alias": "Cog-A", "pos": (2, 4), "age": 6, "seen": 344, "carrying": "clean_plate"},
            {"alias": "Cog-B", "pos": None, "age": 0, "seen": None, "carrying": ""},
            {"alias": "Cog-D", "pos": (3, 9), "age": 41, "seen": 309, "carrying": ""},
        ],
        "board": {"age": 12, "salad": 1, "soup": 2, "fries": 0},
        "stations": [{"name": "pot", "pos": (3, 11), "age": 33, "note": "cooking"}],
        "counters": [((4, 6), "chopped_meat"), ((2, 6), "clean_plate")],
        "blocked": 4,
        "blocked_at": (3, 5),
        "legal_stations": LEGAL,
        "last_order": "station=chop recipe=soup zone=left handoff=Cog-D",
        "radio": [("Cog-A", "I'll take the right side")],
        "note": "A keeps forgetting plates.",
    }
    message = build_user_message(view)
    assert len(message) <= 2000
    assert "LEGAL STATIONS: " + ", ".join(LEGAL) in message
    assert "Cog-B not seen yet" in message
    assert "TEAM RADIO" in message
    assert "YOUR NOTE: A keeps forgetting plates." in message


def test_an_enormous_view_is_still_capped() -> None:
    view = {
        "turn": 1,
        "turns": 18,
        "tick": 1,
        "ticks": 900,
        "layout": "ring",
        "dishes": 0,
        "live": 0,
        "alias": "Cog-A",
        "position": (1, 1),
        "carrying": "",
        "delivered": 0,
        "team": [{"alias": f"Cog-{c}", "pos": (1, 1), "age": 0, "seen": 0, "carrying": "x" * 400} for c in "BCD"],
        "board": {"age": 0, "salad": 0, "soup": 0, "fries": 0},
        "stations": [{"name": "s" * 200, "pos": (1, 1), "age": 0, "note": "n" * 400}] * 9,
        "counters": [((1, 1), "y" * 300)] * 40,
        "blocked": 0,
        "blocked_at": None,
        "legal_stations": LEGAL,
        "last_order": "z" * 500,
        "radio": [("Cog-B", "w" * 500)] * 9,
        "note": "q" * 900,
    }
    assert len(build_user_message(view)) <= 2000

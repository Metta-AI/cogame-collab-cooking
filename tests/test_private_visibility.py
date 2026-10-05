"""Private prompts depend on the issued observation, not unseen spectator state."""

import json

import pytest

from collab_cooking.agent.brain.obs_parser import ObsParser
from collab_cooking.coworld import replay
from collab_cooking.coworld.decision_view import KitchenView
from collab_cooking.coworld.llm import build_user_message
from collab_cooking.coworld.observation import decode_observation
from collab_cooking.coworld.plan_protocol import NativeProfile
from collab_cooking.coworld.teacher import decide
from tests.harness import build_episode


def test_actual_private_window_ignores_unobserved_board_team_inventory_and_heat():
    episode = build_episode({"layout": "open-kitchen", "max_steps": 900})
    episode.state = replay.capture(episode.sim, 4)
    episode.turn = 1
    seat = episode.seats[0]
    seat.kind = "prompt"
    episode._refresh_allies(seat)
    issued = episode.private_observation(0)
    before = episode._seat_view(seat)
    episode.state.board.update({"ticket_hidden_one": 1, "ticket_hidden_two": 1})
    for other in episode.state.cogs[1:]:
        other["carrying"] = "PRIVATE_HIDDEN_INVENTORY"
    episode.heat[(999, 999)] = 10000
    episode._refresh_allies(seat)
    after = episode._seat_view(seat)
    assert episode.private_observation(0) == issued
    assert after == before
    assert after["turn"] == 1
    assert "PRIVATE_HIDDEN_INVENTORY" not in json.dumps(after)
    private = KitchenView.model_validate(before)
    restored = KitchenView.model_validate_json(private.model_dump_json())
    assert build_user_message(restored.model_dump()) == build_user_message(before)
    window = episode.private_window(0, NativeProfile())
    assert window.user == build_user_message(before)
    assert window.observation == private.model_dump(mode="json")
    assert decide(window) == decide(
        window.model_copy(update={"observation": KitchenView.model_validate(after).model_dump(mode="json")})
    )
    with pytest.raises(ValueError, match="private observation"):
        decide(window.model_copy(update={"user": "rewritten private prompt"}))


def test_actual_wire_decoder_preserves_global_tokens():
    episode = build_episode({"layout": "open-kitchen", "max_steps": 900})
    raw = episode.private_observation(0)
    decoded = decode_observation(episode.policy_env, raw, 0)
    known_features = {feature.id for feature in episode.policy_env.obs_features}
    assert [token.raw_token for token in decoded.tokens] == [
        tuple(token) for token in raw if token[1] in known_features
    ]
    parser = ObsParser(episode.policy_env)
    actual, _ = parser.parse(episode.sim.agent(0).observation, fallback_position=(0, 0))
    received, _ = parser.parse(decoded, fallback_position=(0, 0))
    assert received == actual


def test_cpp_hidden_inventory_permutation_preserves_issued_observation_and_prompt():
    left = build_episode({"layout": "figure-eight", "max_steps": 900, "seed": 9})
    right = build_episode({"layout": "figure-eight", "max_steps": 900, "seed": 9})
    left.sim.agent(1).set_inventory({"veg": 1})
    right.sim.agent(1).set_inventory({"meat": 1})
    for episode in (left, right):
        for slot in range(4):
            episode.sim.agent(slot).set_action("noop")
        episode.sim.step()
        episode.state = replay.capture(episode.sim, 4)
        episode.turn = 1
        episode.seats[0].kind = "prompt"
        episode._refresh_allies(episode.seats[0])
    assert left.sim.agent(1).inventory != right.sim.agent(1).inventory
    _, visible = left._obs_parser.parse(left.sim.agent(0).observation, fallback_position=left.state.cogs[0]["pos"])
    assert left.state.cogs[1]["pos"] not in visible
    assert left.private_observation(0) == right.private_observation(0)
    left_view = left._seat_view(left.seats[0])
    right_view = right._seat_view(right.seats[0])
    assert left_view == right_view
    assert build_user_message(left_view) == build_user_message(right_view)


def test_cpp_visible_inventory_is_preserved_from_actual_private_tokens():
    episode = build_episode({"layout": "open-kitchen", "max_steps": 900, "seed": 9})
    episode.sim.agent(1).set_inventory({"veg": 1})
    for slot in range(4):
        episode.sim.agent(slot).set_action("noop")
    episode.sim.step()
    episode.state = replay.capture(episode.sim, 4)
    seat = episode.seats[0]
    _, visible = episode._obs_parser.parse(
        episode.sim.agent(0).observation, fallback_position=episode.state.cogs[0]["pos"]
    )
    teammate_position = episode.state.cogs[1]["pos"]
    assert visible[teammate_position].properties["veg"] == 1
    episode.state.cogs[1]["carrying"] = "PRIVATE_GLOBAL_INVENTORY_SENTINEL"
    episode._refresh_allies(seat)
    assert seat.ally_memory[episode.seats[1].alias][2] == "veg"
    assert "PRIVATE_GLOBAL_INVENTORY_SENTINEL" not in json.dumps(episode._seat_view(seat))

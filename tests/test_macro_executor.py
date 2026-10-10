"""The actual ordinary controller and its verifier consume identical private ticks."""

import copy
from uuid import uuid4

import pytest

from collab_cooking.coworld.macro_executor import MacroExecutor
from collab_cooking.coworld.plan_protocol import DirectiveInstalled, PhysicalAction, PhysicalObservation
from collab_cooking.coworld.plans import parse_plan
from tests.harness import build_episode


def test_actual_engine_private_ticks_match_shipped_executor_without_double_advance():
    episode = build_episode({"layout": "open-kitchen", "max_steps": 900})
    verifier = MacroExecutor(episode.policy_env, 0, "open-kitchen", "brigade")
    player = MacroExecutor(episode.policy_env, 0, "open-kitchen", "brigade")
    plan = parse_plan('{"station":"veg","note":"PRIVATE_DIRECTIVE"}', ["veg"], ["none"])
    directive = DirectiveInstalled(window_id=uuid4(), slot=0, turn=1, plan=plan, cause="")
    verifier.install(directive)
    player.install(directive)
    for step in range(30):
        raw = episode.private_observation(0)
        unchanged = copy.deepcopy(raw)
        issued = PhysicalObservation(request_id=uuid4(), slot=0, step=step, observation=raw)
        verifier.issue(issued)
        player.issue(issued)
        actual = player.expected
        player.admit(PhysicalAction(request_id=issued.request_id, slot=0, action_name=actual))
        assert episode.sim.current_step == step
        assert raw == unchanged and issued.observation == unchanged
        admitted = verifier.admit(PhysicalAction(request_id=issued.request_id, slot=0, action_name=actual))
        assert admitted.expected_action == admitted.submitted_action == admitted.installed_action == actual
        assert admitted.cause == "" and admitted.directive_window_id == directive.window_id
        assert episode.sim.current_step == step
        for slot in range(4):
            episode.sim.agent(slot).set_action(actual if slot == 0 else "noop")
        episode.sim.step()
        assert episode.sim.current_step == step + 1
    assert verifier.state.step == player.state.step == 30


def test_rewritten_or_stale_physical_controls_cannot_install_a_different_action():
    episode = build_episode({"layout": "open-kitchen", "max_steps": 900})
    verifier = MacroExecutor(episode.policy_env, 0, "open-kitchen", "brigade")
    issued = PhysicalObservation(request_id=uuid4(), slot=0, step=0, observation=episode.private_observation(0))
    verifier.issue(issued)
    with pytest.raises(ValueError, match="overlaps"):
        verifier.issue(issued)
    with pytest.raises(ValueError, match="identity"):
        verifier.admit(PhysicalAction(request_id=uuid4(), slot=0, action_name=verifier.expected))
    with pytest.raises(ValueError, match="identity"):
        verifier.admit(PhysicalAction(request_id=issued.request_id, slot=1, action_name=verifier.expected))
    altered = "move_north" if verifier.expected != "move_north" else "move_south"
    denied = verifier.admit(PhysicalAction(request_id=issued.request_id, slot=0, action_name=altered))
    assert denied.submitted_action == altered and denied.installed_action == "noop"
    assert denied.cause == "executor_mismatch"
    assert episode.sim.current_step == 0
    with pytest.raises(ValueError, match="no issued"):
        verifier.admit(PhysicalAction(request_id=issued.request_id, slot=0, action_name=altered))

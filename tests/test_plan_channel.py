"""The authenticated engine boundary denies forged controls and stop credit."""

import base64
from uuid import uuid4

import pytest

from collab_cooking.coworld.plan_admission import PlanAdmission, PlayerRoster
from collab_cooking.coworld.plan_channel import PLAYER_FRAME, PlanChannel
from collab_cooking.coworld.plan_protocol import PhysicalAction, ScriptedRegistration, Stopped
from tests.harness import build_episode
from tests.test_plan_admission import progress, window


@pytest.mark.asyncio
async def test_engine_issued_physical_controls_and_nonce_bound_stop_credit():
    episode = build_episode({"layout": "open-kitchen", "max_steps": 900})
    roster = PlayerRoster()
    admission = PlanAdmission(26)
    channels = [
        PlanChannel(slot, roster, admission, episode.policy_env, "open-kitchen", "brigade") for slot in range(4)
    ]
    for channel in channels:
        await channel.receive(
            PLAYER_FRAME.validate_python(ScriptedRegistration(slot=channel.slot, baseline="brigade").model_dump())
        )
    roster.freeze()
    channel = channels[0]
    observation = channel.observe(episode.private_observation(0), 0)
    wrong = PhysicalAction(request_id=uuid4(), slot=0, action_name="noop")
    with pytest.raises(ValueError, match="current private window"):
        await channel.receive(wrong)
    valid = wrong.model_copy(update={"request_id": observation.request_id, "action_name": channel.executor.expected})
    await channel.receive(valid)
    with pytest.raises(ValueError, match="repeats"):
        await channel.receive(valid)
    admitted = channel.admit("deadline")
    assert admitted.installed_action == valid.action_name and admitted.cause == ""
    with pytest.raises(ValueError, match="matching engine stop"):
        await channel.receive(Stopped(request_id=uuid4(), joined=True))
    stop = channel.stop()
    assert channel.stop() == stop
    with pytest.raises(ValueError, match="follows engine stop"):
        await channel.receive(valid)
    await channel.receive(Stopped(request_id=stop.request_id, joined=True))
    assert channel.evidence_received().request_id == stop.request_id
    with pytest.raises(ValueError, match="repeats engine stop"):
        await channel.receive(Stopped(request_id=stop.request_id, joined=True))


@pytest.mark.asyncio
async def test_stop_never_credits_unresolved_reader_and_retains_latest_prefix():
    episode = build_episode({"layout": "open-kitchen", "max_steps": 900})
    admission = PlanAdmission(26)
    channel = PlanChannel(0, PlayerRoster(), admission, episode.policy_env, "open-kitchen", "brigade")
    grant = admission.begin([window(0)], 12)[0]
    complete = progress(grant, '{"station":"pass"}')
    partial = complete.model_copy(
        update={
            "evidence": complete.evidence.model_copy(
                update={
                    "response_body_b64": base64.b64encode(b"PRIVATE_PARTIAL_PREFIX").decode(),
                    "raw_response": "PRIVATE_PARTIAL_PREFIX",
                    "response_complete": False,
                    "response_reader_joined": False,
                    "latency_ms": 1,
                }
            )
        }
    )
    await channel.receive(partial)
    stop = channel.stop()
    admission.stop()
    with pytest.raises(ValueError, match="unresolved native ownership"):
        await channel.receive(Stopped(request_id=stop.request_id, joined=True))
    assert not channel.acknowledged
    assert (
        admission.history[grant.window.window_id].attempts[grant.attempt_id].response_body_b64
        == partial.evidence.response_body_b64
    )
    with pytest.raises(ValueError, match="matching joined engine stop"):
        channel.evidence_received()


@pytest.mark.asyncio
async def test_external_research_controls_remain_legal_without_fabricated_macro_authority():
    from collab_cooking.coworld.plan_protocol import ExternalRegistration

    episode = build_episode({"layout": "open-kitchen", "max_steps": 900})
    roster = PlayerRoster()
    admission = PlanAdmission(26)
    channel = PlanChannel(0, roster, admission, episode.policy_env, "open-kitchen", "brigade")
    await channel.receive(PLAYER_FRAME.validate_python(ExternalRegistration(slot=0).model_dump()))
    for slot in range(1, 4):
        roster.register(slot, ScriptedRegistration(slot=slot, baseline="brigade"))
    roster.freeze()
    observation = channel.observe(episode.private_observation(0), 0)
    await channel.receive(PhysicalAction(request_id=observation.request_id, slot=0, action_name="move_north"))
    admitted = channel.admit("deadline")
    assert admitted.installed_action == "move_north" and admitted.cause == ""
    assert admitted.expected_action is None and admitted.directive_window_id is None
    assert admission.history == {}
    episode.sim.agent(0).set_action(admitted.installed_action)
    for slot in range(1, 4):
        episode.sim.agent(slot).set_action("noop")
    episode.sim.step()
    observation = channel.observe(episode.private_observation(0), 1)
    await channel.receive(PhysicalAction(request_id=observation.request_id, slot=0, action_name="invalid_control"))
    rejected = channel.admit("deadline")
    assert rejected.installed_action == "noop" and rejected.cause == "invalid_action"

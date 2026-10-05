"""Source-issued controls retain exact private inputs and bounded native contracts."""

from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from collab_cooking.coworld.plan_admission import PlayerRoster
from collab_cooking.coworld.plan_protocol import (
    NativeProfile,
    NativeRegistration,
    PhysicalAction,
    PhysicalObservation,
    Registration,
    ScriptedRegistration,
)


def test_policy_registration_preserves_raw_strategy_without_teacher_authority():
    registration = NativeRegistration(slot=2, prompt="  private strategy\n", profile=NativeProfile())
    assert registration.prompt == "  private strategy\n"
    assert TypeAdapter(Registration).validate_json(registration.model_dump_json()) == registration
    with pytest.raises(ValidationError) as invalid:
        NativeRegistration.model_validate(
            registration.model_dump() | {"origin": "teacher", "private": "private-ingress-sentinel"}
        )
    assert "private-ingress-sentinel" not in str(invalid.value)
    with pytest.raises(ValidationError):
        NativeRegistration(slot=True, prompt="strategy", profile=NativeProfile())
    with pytest.raises(ValidationError):
        NativeRegistration(slot=2, prompt="x" * 1201, profile=NativeProfile())


def test_physical_reply_is_separate_from_issued_private_observation():
    request_id = uuid4()
    observation = PhysicalObservation(request_id=request_id, slot=2, step=50, observation=[[255, 1, 7], [33, 2, 4]])
    assert observation.observation[0] == [255, 1, 7]
    action = PhysicalAction(request_id=request_id, slot=2, action_name="move_north")
    assert action.request_id == observation.request_id
    with pytest.raises(ValidationError):
        PhysicalAction.model_validate(action.model_dump() | {"policy_infos": {"task": "private task"}})
    with pytest.raises(ValidationError):
        PhysicalObservation.model_validate(observation.model_dump() | {"observation": [[False, 1, 7]]})


def test_authenticated_roster_rejects_duplicate_late_and_wrong_seat_registration():
    roster = PlayerRoster()
    original = NativeRegistration(slot=2, prompt="  private strategy\n", profile=NativeProfile())
    with pytest.raises(ValueError, match="authenticated seat"):
        roster.register(1, original)
    roster.register(2, original)
    with pytest.raises(ValueError, match="closed"):
        roster.register(2, original.model_copy(update={"prompt": "replacement"}))
    with pytest.raises(ValueError, match="frozen"):
        roster.player(2)
    with pytest.raises(ValueError, match="all four"):
        roster.freeze()
    for slot in (0, 1, 3):
        roster.register(slot, ScriptedRegistration(slot=slot, baseline="brigade"))
    roster.freeze()
    assert roster.player(2) == original
    with pytest.raises(ValueError, match="closed"):
        roster.register(2, original.model_copy(update={"profile": NativeProfile(temperature=0.0)}))
    with pytest.raises(ValueError, match="already frozen"):
        roster.freeze()

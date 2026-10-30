"""Actual received completion, source request grants, and independent plan authority."""

import base64
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from collab_cooking.coworld.native import Attempt, Usage
from collab_cooking.coworld.plan_admission import PlanAdmission
from collab_cooking.coworld.plan_protocol import AttemptProgress, NativeProfile, PlanReply, PlanWindow
from collab_cooking.coworld.plans import parse_plan


def window(slot):
    return PlanWindow(
        window_id=uuid4(),
        slot=slot,
        turn=1,
        step=50,
        alias=f"Cog-{slot}",
        system="Ordinary private kitchen plan",
        user="Observed board soup 1",
        legal_stations=["board", "pass"],
        ally_aliases=["none"],
        observation={"tick": 50, "board": {"soup": 1}},
        profile=NativeProfile(),
    )


def progress(grant, text):
    raw = json.dumps(
        {
            "model": grant.window.profile.model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 12, "output_tokens": 9},
        }
    )
    evidence = Attempt(
        attempt_id=str(grant.attempt_id),
        slot=grant.window.slot,
        stage="plan",
        request=grant.window.native_request(grant.user),
        timeout_ms=grant.timeout_seconds * 1000,
        endpoint="http://127.0.0.1:8080",
        response=text,
        raw_response=raw,
        response_body_b64=base64.b64encode(raw.encode()).decode(),
        http_status=200,
        response_complete=True,
        response_reader_joined=True,
        model=grant.window.profile.model,
        usage=Usage(input_tokens=12, output_tokens=9),
        stop_reason="end_turn",
    )
    return AttemptProgress(window_id=grant.window.window_id, attempt_id=grant.attempt_id, evidence=evidence)


@pytest.mark.asyncio
async def test_received_response_must_parse_to_separately_submitted_macro():
    admission = PlanAdmission(26)
    grant = admission.begin([window(0)], 12)[0]
    text = '{"station":"pass","say":"public","note":"PRIVATE_SENTINEL"}'
    admission.progress(0, progress(grant, text))
    actual = parse_plan(text, grant.window.legal_stations, grant.window.ally_aliases)
    message = PlanReply(
        window_id=grant.window.window_id,
        attempt_id=grant.attempt_id,
        response=text,
        submitted_plan=actual.model_copy(update={"station": "board"}),
        rejection_reason=None,
        reader_joined=True,
    )
    with pytest.raises(ValueError, match="independently parsed"):
        await admission.reply(0, message)
    parsed = await admission.reply(0, message.model_copy(update={"submitted_plan": actual}))
    assert parsed == actual
    assert "PRIVATE_SENTINEL" not in json.dumps(parsed.replay_event(0, "Cog-A", 1, "llm"))
    with pytest.raises(ValueError, match="inactive"):
        admission.progress(0, progress(grant, text))


@pytest.mark.asyncio
async def test_received_model_and_usage_cannot_disagree_with_caller_body():
    admission = PlanAdmission(26)
    grant = admission.begin([window(0)], 12)[0]
    text = '{"station":"pass"}'
    message = progress(grant, text)
    message = message.model_copy(update={"evidence": message.evidence.model_copy(update={"model": "forged-model"})})
    admission.progress(0, message)
    reply = PlanReply(
        window_id=grant.window.window_id,
        attempt_id=grant.attempt_id,
        response=text,
        submitted_plan=parse_plan(text, grant.window.legal_stations, grant.window.ally_aliases),
        rejection_reason=None,
        reader_joined=True,
    )
    with pytest.raises(ValueError, match="response metadata"):
        await admission.reply(0, reply)


def test_observed_header_identity_and_normalized_headers_are_immutable():
    admission = PlanAdmission(26)
    grant = admission.begin([window(0)], 12)[0]
    first = progress(grant, '{"station":"pass"}')
    first = first.model_copy(
        update={
            "evidence": first.evidence.model_copy(
                update={
                    "response_reader_joined": False,
                    "response_complete": False,
                    "platform_call_id": str(uuid4()),
                    "model_identity": "actual-header-identity",
                    "response_headers": {"x-coworld-checkpoint-sha256": "actual-header-identity"},
                }
            )
        }
    )
    admission.progress(0, first)
    for mutation in (
        {"model_identity": "replacement"},
        {"platform_call_id": str(uuid4())},
        {"response_headers": {"x-coworld-checkpoint-sha256": "replacement"}},
    ):
        changed = first.model_copy(update={"evidence": first.evidence.model_copy(update=mutation)})
        with pytest.raises(ValueError, match="rewrites observed"):
            admission.progress(0, changed)


@pytest.mark.asyncio
async def test_retry_requires_actual_rejection_and_consumes_shared_budget():
    admission = PlanAdmission(5)
    grants = admission.begin([window(slot) for slot in range(4)], 12)
    assert len(grants) == 4 and len(admission.events) == 4
    assert len({entry.deadline for entry in admission.windows.values()}) == 1
    with pytest.raises(ValueError, match="source-observed rejection"):
        admission.grant(0)
    for grant in grants[:2]:
        admission.progress(grant.window.slot, progress(grant, "not a plan"))
        reply = PlanReply(
            window_id=grant.window.window_id,
            attempt_id=grant.attempt_id,
            response="not a plan",
            submitted_plan=None,
            rejection_reason="claimed transport failure",
            reader_joined=True,
        )
        assert await admission.reply(grant.window.slot, reply) is None
        assert admission.windows[grant.window.slot].rejection_reason == "parse"
    retry = admission.grant(0)
    assert retry.attempt_index == 1 and retry.user.startswith(grants[0].user)
    assert admission.grant(1) is None
    assert admission.windows[1].closed and admission.windows[1].rejection_reason == "rate_budget"
    assert len(admission.events) == 5


def test_progress_cannot_forge_request_identity_or_rewrite_received_prefix():
    admission = PlanAdmission(26)
    grant = admission.begin([window(0)], 12)[0]
    received = progress(grant, '{"station":"pass"}')
    with pytest.raises(ValueError, match="issued request"):
        admission.progress(
            0, received.model_copy(update={"evidence": received.evidence.model_copy(update={"slot": 1})})
        )
    partial = received.evidence.model_copy(
        update={
            "response_complete": False,
            "response_reader_joined": False,
            "response_body_b64": base64.b64encode(b'{"model"').decode(),
            "raw_response": '{"model"',
        }
    )
    admission.progress(0, received.model_copy(update={"evidence": partial}))
    bad = received.evidence.model_copy(update={"response_body_b64": base64.b64encode(b"changed").decode()})
    with pytest.raises(ValueError, match="byte prefix"):
        admission.progress(0, received.model_copy(update={"evidence": bad}))
    admission.progress(0, received)
    with pytest.raises(ValueError, match="joined response"):
        admission.progress(
            0, received.model_copy(update={"evidence": received.evidence.model_copy(update={"http_status": 503})})
        )
    admission.stop()
    admission.progress(0, received)
    admission.seal()
    with pytest.raises(ValueError, match="inactive"):
        admission.progress(0, received)


def test_stop_retains_final_owned_progress_but_never_seals_unresolved_reader():
    admission = PlanAdmission(26)
    grant = admission.begin([window(0)], 12)[0]
    received = progress(grant, '{"station":"pass"}')
    partial = received.evidence.model_copy(
        update={
            "response_complete": False,
            "response_reader_joined": False,
            "response_body_b64": "",
            "raw_response": "",
        }
    )
    admission.progress(0, received.model_copy(update={"evidence": partial}))
    admission.stop()
    with pytest.raises(ValueError, match="unresolved reader"):
        admission.seal()
    admission.progress(0, received)
    admission.seal()
    assert admission.windows[0].attempts[grant.attempt_id].response_reader_joined is True
    with pytest.raises(ValueError, match="inactive"):
        admission.progress(0, received)


@pytest.mark.asyncio
async def test_settled_no_response_failure_keeps_unobserved_http_fields_and_permits_retry():
    admission = PlanAdmission(26)
    grant = admission.begin([window(0)], 12)[0]
    started = Attempt(
        attempt_id=str(grant.attempt_id),
        slot=0,
        stage="plan",
        request=grant.window.native_request(grant.user),
        timeout_ms=grant.timeout_seconds * 1000,
        endpoint="http://127.0.0.1:8080",
        latency_ms=12,
        rejection_reason="ConnectionError",
    )
    admission.progress(
        0, AttemptProgress(window_id=grant.window.window_id, attempt_id=grant.attempt_id, evidence=started)
    )
    message = PlanReply(
        window_id=grant.window.window_id,
        attempt_id=grant.attempt_id,
        response=None,
        submitted_plan=None,
        rejection_reason="ConnectionError",
        reader_joined=True,
    )
    assert await admission.reply(0, message) is None
    assert admission.windows[0].rejection_reason == "transport"
    assert admission.grant(0).attempt_index == 1
    assert started.response_complete is None and started.response_reader_joined is None
    assert started.http_status is None and started.response_body_b64 is None


def test_late_received_prefix_survives_next_batch_and_blocks_seal_until_joined():
    admission = PlanAdmission(26)
    first = admission.begin([window(0)], 12)[0]
    complete = progress(first, '{"station":"pass"}')
    partial = complete.model_copy(
        update={
            "evidence": complete.evidence.model_copy(
                update={
                    "response_complete": False,
                    "response_reader_joined": False,
                    "response_body_b64": "",
                    "raw_response": "",
                }
            )
        }
    )
    admission.progress(0, partial)
    admission.windows[0].closed = True
    second = admission.begin([window(0)], 12)[0]
    assert second.window.window_id != first.window.window_id
    admission.progress(0, partial)
    admission.stop()
    with pytest.raises(ValueError, match="unresolved reader"):
        admission.seal()
    admission.progress(0, complete)
    admission.seal()
    assert admission.history[first.window.window_id].attempts[first.attempt_id] == complete.evidence
    assert len(admission.history) == 2


def test_wire_validation_preserves_invalid_utf8_and_rejects_conflicting_received_text():
    admission = PlanAdmission(26)
    grant = admission.begin([window(0)], 12)[0]
    received = progress(grant, '{"station":"pass"}')
    packet = received.model_dump(mode="json")
    packet["evidence"].update(
        raw_response=None,
        response_body_b64=base64.b64encode(b'{"text":"\xe2\x82').decode(),
        response_complete=False,
        response_reader_joined=False,
    )
    private = AttemptProgress.model_validate_json(json.dumps(packet))
    admission.progress(0, private)
    assert base64.b64decode(admission.windows[0].attempts[grant.attempt_id].response_body_b64) == b'{"text":"\xe2\x82'
    packet["evidence"]["raw_response"] = "invented complete text"
    with pytest.raises(ValidationError, match="actual received bytes"):
        AttemptProgress.model_validate_json(json.dumps(packet))


@pytest.mark.asyncio
async def test_unissued_window_and_unobserved_call_cannot_admit_a_native_reply():
    admission = PlanAdmission(26)
    grant = admission.begin([window(0)], 12)[0]
    received = progress(grant, '{"station":"pass"}')
    with pytest.raises(ValueError, match="issued private window"):
        admission.progress(0, received.model_copy(update={"window_id": uuid4()}))
    reply = PlanReply(
        window_id=grant.window.window_id,
        attempt_id=grant.attempt_id,
        response='{"station":"pass"}',
        submitted_plan=parse_plan('{"station":"pass"}', ["pass"], ["none"]),
        rejection_reason=None,
        reader_joined=True,
    )
    with pytest.raises(ValueError, match="observed granted call"):
        await admission.reply(0, reply)


@pytest.mark.asyncio
async def test_old_received_attempts_replay_exactly_from_private_spool_until_owned_seal():
    import os

    admission = PlanAdmission(26)
    entries = []
    snapshots = []
    for turn in range(1, 4):
        issued = window(0).model_copy(update={"turn": turn})
        grant = admission.begin([issued], 12)[0]
        text = '{"station":"pass","say":"public","note":"PRIVATE_SPOOL_' + str(turn) + '"}'
        message = progress(grant, text)
        admission.progress(0, message)
        submitted = parse_plan(text, issued.legal_stations, issued.ally_aliases)
        assert (
            await admission.reply(
                0,
                PlanReply(
                    window_id=issued.window_id,
                    attempt_id=grant.attempt_id,
                    response=text,
                    submitted_plan=submitted,
                    rejection_reason=None,
                    reader_joined=True,
                ),
            )
            == submitted
        )
        entry = admission.windows[0]
        entries.append(entry)
        snapshots.append((grant.attempt_id, message.evidence))
        assert os.fstat(entry.attempts.stream.fileno()).st_mode & 0o777 == 0o600
    for entry, (identity, received) in zip(entries, snapshots, strict=True):
        assert entry.attempts[identity] == received
    admission.stop()
    admission.seal()
    assert all(entry.attempts.stream.closed for entry in entries)

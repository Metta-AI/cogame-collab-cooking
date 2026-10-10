"""Actual parser/application joins and private atomic canonical publication."""

import asyncio
import base64
import json
import sys
from uuid import uuid4

import pytest
from coworld.decision_trajectory import CompleteEpisode

from collab_cooking.coworld import lifecycle
from collab_cooking.coworld.macro_executor import AdmittedControl
from collab_cooking.coworld.native import Attempt, Usage
from collab_cooking.coworld.plan_protocol import (
    DirectiveInstalled,
    NativeProfile,
    NativeRegistration,
    PlanWindow,
    RequestGrant,
)
from collab_cooking.coworld.plans import parse_plan
from collab_cooking.coworld.private_journal import (
    EngineApplication,
    EngineOutcome,
    GrantedCall,
    InstalledMacro,
    Journal,
    OwnershipState,
    PrivateWindow,
    ReceivedCall,
    RegisteredPolicies,
    SourceIdentity,
)
from collab_cooking.coworld.trajectory import finalize, finalize_owned


def source(tmp_path, *, apply=True, joined=True, forged=False):
    journal = Journal(
        tmp_path / "private.evidence.partial.jsonl",
        SourceIdentity(episode_id="actual-private-episode", source_revision="a" * 40, game_version="diagnostic"),
        {"seed": 1, "layout": "cramped", "step_seconds": 0.2},
    )
    journal.append(
        RegisteredPolicies(
            registrations=[
                NativeRegistration(slot=slot, prompt="private strategy", profile=NativeProfile()) for slot in range(4)
            ]
        )
    )
    window = PlanWindow(
        window_id=uuid4(),
        slot=0,
        turn=1,
        step=0,
        alias="Cog-A",
        system="private system",
        user="private board",
        legal_stations=["board", "pass"],
        ally_aliases=["none"],
        observation={"board": "private"},
        profile=NativeProfile(),
    )
    journal.append(PrivateWindow(window=window))
    text = '{"station":"board","note":"PRIVATE_SENTINEL"}'
    plan = parse_plan(text, window.legal_stations, window.ally_aliases)
    raw = json.dumps(
        {
            "model": "actual-served-model",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 8},
        }
    )
    attempt = Attempt(
        slot=0,
        stage="plan",
        request=window.native_request(window.user),
        response=text,
        raw_response=raw,
        response_body_b64=base64.b64encode(raw.encode()).decode(),
        response_complete=True,
        response_reader_joined=True,
        http_status=200,
        model="actual-served-model",
        usage=Usage(input_tokens=3, output_tokens=8),
        stop_reason="end_turn",
        platform_call_id=str(uuid4()),
        timeout_ms=12000,
    )
    journal.append(
        GrantedCall(
            grant=RequestGrant(
                window=window, attempt_id=attempt.attempt_id, attempt_index=0, user=window.user, timeout_seconds=12
            )
        )
    )
    journal.append(ReceivedCall(window_id=str(window.window_id), attempt=attempt))
    journal.append(
        InstalledMacro(
            directive=DirectiveInstalled(
                window_id=window.window_id,
                slot=0,
                turn=1,
                plan=plan.model_copy(update={"station": "pass"}) if forged else plan,
                cause="",
            ),
            selected_attempt_id=attempt.attempt_id,
        )
    )
    if apply:
        journal.append(
            EngineApplication(
                start_tick=0,
                end_tick=1,
                controls=[
                    AdmittedControl(
                        request_id=uuid4(),
                        slot=slot,
                        step=0,
                        directive_window_id=window.window_id if slot == 0 else None,
                        expected_action="noop",
                        submitted_action="noop",
                        installed_action="noop",
                        cause="",
                    )
                    for slot in range(4)
                ],
                actions=[0, 0, 0, 0],
                vibe_actions=[0, 0, 0, 0],
                effects=[],
                scores=[0, 0, 0, 0],
            )
        )
    journal.append(EngineOutcome(reason="complete", results={"scores": [0, 0, 0, 0]}))
    for scope in ("engine", "players", "episode"):
        journal.append(OwnershipState(scope=scope, joined=joined, reason="actual owner readback"))
    journal.close_joined()
    return journal


def test_joined_native_macro_preserves_actual_text_bytes_and_engine_controls(tmp_path):
    journal = source(tmp_path)
    finalize(journal.path, journal.canonical_path)
    complete = CompleteEpisode.model_validate_json(journal.canonical_path.read_bytes())
    decision = complete.decisions[0]
    assert decision.action_status == "accepted"
    assert decision.execution.control_encoding == "u8-u8"
    assert base64.b64decode(decision.execution.seat_controls_b64) == b"\0\0"
    assert decision.attempts[0].model == "actual-served-model"
    assert "PRIVATE_SENTINEL" in decision.attempts[0].response
    assert decision.attempts[0].raw_response.encode() == base64.b64decode(decision.attempts[0].response_body_b64)
    assert journal.canonical_path.stat().st_mode & 0o777 == 0o600
    assert complete.episode.outcome["physical_evidence_file"] == journal.path.name
    with pytest.raises(FileExistsError):
        finalize(journal.path, journal.canonical_path)


def test_installed_but_never_applied_macro_is_not_a_label(tmp_path):
    journal = source(tmp_path, apply=False)
    finalize(journal.path, journal.canonical_path)
    decision = CompleteEpisode.model_validate_json(journal.canonical_path.read_bytes()).decisions[0]
    assert not decision.attempts[0].accepted
    assert decision.execution is None
    assert decision.executed_action is None
    assert decision.action_status == "fallback"


@pytest.mark.parametrize("change", ["unjoined", "forged", "late-invalid"])
def test_invalid_private_episode_has_no_canonical_publication(tmp_path, change):
    journal = source(tmp_path, joined=change != "unjoined", forged=change == "forged")
    if change == "late-invalid":
        with journal.path.open("ab") as raw:
            raw.write(b'{"kind":"ownership","scope":"episode","joined":false,"reason":"late unresolved owner"}\n')
    with pytest.raises(ValueError):
        finalize(journal.path, journal.canonical_path)
    assert not journal.canonical_path.exists()
    assert not list(tmp_path.glob("tmp*"))


@pytest.mark.asyncio
async def test_owned_finalizer_observes_os_exit_before_canonical_admission(tmp_path):
    journal = source(tmp_path)
    await finalize_owned(journal.path, journal.canonical_path)
    complete = CompleteEpisode.model_validate_json(journal.canonical_path.read_bytes())
    assert complete.decisions[0].action_status == "accepted"
    assert journal.canonical_path.with_name(journal.canonical_path.name + ".candidate.jsonl").exists()


@pytest.mark.asyncio
async def test_cancelled_finalizer_kills_and_joins_actual_writer_without_publication(tmp_path, monkeypatch):
    journal = source(tmp_path)
    original = asyncio.create_subprocess_exec
    acquired = asyncio.Event()
    children = []

    async def stalled(*command, **options):
        child = await original(sys.executable, "-c", "import time; time.sleep(60)", **options)
        children.append(child)
        acquired.set()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", stalled)
    task = asyncio.create_task(finalize_owned(journal.path, journal.canonical_path))
    await acquired.wait()
    task.cancel()
    assert await lifecycle.settle({task}, asyncio.get_running_loop().time() + 2, cancel=False)
    assert task.cancelled()
    assert children[0].returncode is not None
    assert not journal.canonical_path.exists()
    assert journal.path.exists()

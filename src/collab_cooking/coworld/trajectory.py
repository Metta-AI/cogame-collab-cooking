"""Joined source journal to bounded, private canonical decision evidence.

Physical observations and effects remain in the immutable auxiliary journal.
Each selected macro carries only its own actual admitted control interval.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Annotated, BinaryIO
from uuid import UUID

from coworld.decision_trajectory import DecisionAttempt, DecisionEvent, EpisodeEvent
from pydantic import Field, TypeAdapter

from . import lifecycle
from .native import Attempt, Response
from .plan_protocol import NativeRegistration, PlanControl
from .plans import parse_plan
from .private_journal import (
    RECORD,
    EngineApplication,
    EngineOutcome,
    GrantedCall,
    InstalledMacro,
    OwnershipState,
    PrivateWindow,
    ReceivedCall,
    RegisteredPolicies,
    SourceRecord,
    TeacherCall,
)
from .teacher import TEACHER_POLICY, decide

MAX_JOURNAL_LINE = 16 * 1024 * 1024
STEP_SECONDS = TypeAdapter(Annotated[float, Field(gt=0, allow_inf_nan=False)])


def records(stream: BinaryIO):
    """Bounded typed framing, retaining exact byte offsets for private rereads."""
    while True:
        offset = stream.tell()
        line = stream.readline(MAX_JOURNAL_LINE + 1)
        if not line:
            return
        if len(line) > MAX_JOURNAL_LINE or not line.endswith(b"\n"):
            raise ValueError("private journal line exceeds capacity or lacks its terminator")
        yield offset, RECORD.validate_json(line)


def model_attempt(evidence: Attempt, policy: str, accepted: bool, action: PlanControl | None) -> DecisionAttempt:
    sample = evidence.sampling_evidence
    request = evidence.request.model_dump(mode="json")
    return DecisionAttempt.model_validate(
        {
            "attempt_id": evidence.attempt_id,
            "platform_call_id": evidence.platform_call_id,
            "policy": policy,
            "origin": "model",
            "inference_mode": evidence.inference_mode,
            "prompt": [{"role": "system", "content": request["system"]}, *request["messages"]],
            "request": request,
            "response": evidence.response,
            "raw_response": evidence.raw_response,
            "response_headers": evidence.response_headers or None,
            "response_body_b64": evidence.response_body_b64,
            "response_complete": evidence.response_complete,
            "response_reader_joined": evidence.response_reader_joined,
            "http_status": evidence.http_status,
            "provider_request_id": evidence.provider_request_id,
            "model": evidence.model,
            "model_identity": evidence.model_identity,
            "tokenizer_identity": evidence.tokenizer_identity,
            "chat_template_sha256": evidence.chat_template_sha256,
            "decoder": {
                "max_tokens": evidence.request.max_tokens,
                "temperature": evidence.request.temperature,
                "top_p": evidence.request.top_p,
                "timeout_ms": evidence.timeout_ms,
            },
            "prompt_token_ids": None if sample is None else sample.prompt_token_ids,
            "sampled_token_ids": None if sample is None else sample.completion_token_ids,
            "stop_reason": evidence.stop_reason,
            "parsed_action": None if action is None else action.model_dump(mode="json"),
            "accepted": accepted,
            "rejection_reason": None if accepted else evidence.rejection_reason or "not_selected_or_not_applied",
            "latency_ms": evidence.latency_ms,
            "input_tokens": None if evidence.usage is None else evidence.usage.input_tokens,
            "output_tokens": None if evidence.usage is None else evidence.usage.output_tokens,
            "behavior_logprobs": None if sample is None else sample.behavior_log_probs,
        }
    )


def finalize(source: Path, destination: Path) -> None:
    """Publish once after full ownership/terminal validation, never from a live writer."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as raw, tempfile.TemporaryDirectory(prefix="collab-index-", dir=destination.parent) as work:
        os.chmod(work, 0o700)
        with closing(sqlite3.connect(Path(work) / "index.sqlite")) as index:
            index.executescript(
                "CREATE TABLE windows(id TEXT PRIMARY KEY, offset INTEGER, installation INTEGER);"
                "CREATE TABLE attempts(id TEXT PRIMARY KEY, window TEXT, offset INTEGER);"
                "CREATE TABLE grants(id TEXT PRIMARY KEY, window TEXT, offset INTEGER);"
                "CREATE TABLE controls(window TEXT, tick INTEGER, action INTEGER, vibe INTEGER, cause TEXT);"
            )
            source_record: SourceRecord | None = None
            outcome: EngineOutcome | None = None
            policies: RegisteredPolicies | None = None
            joined: dict[str, bool] = {}
            last_tick = -1
            for offset, record in records(raw):
                if isinstance(record, SourceRecord):
                    if source_record is not None or offset != 0:
                        raise ValueError("private source identity must occur exactly once at the beginning")
                    source_record = record
                elif isinstance(record, RegisteredPolicies):
                    if policies is not None or sorted(item.slot for item in record.registrations) != [0, 1, 2, 3]:
                        raise ValueError("private policy roster must contain every assigned seat exactly once")
                    policies = record
                elif isinstance(record, PrivateWindow):
                    index.execute("INSERT INTO windows VALUES(?,?,NULL)", (str(record.window.window_id), offset))
                elif isinstance(record, GrantedCall):
                    index.execute(
                        "INSERT INTO grants VALUES(?,?,?)",
                        (str(record.grant.attempt_id), str(record.grant.window.window_id), offset),
                    )
                elif isinstance(record, InstalledMacro):
                    window = str(record.directive.window_id)
                    changed = index.execute(
                        "UPDATE windows SET installation=? WHERE id=? AND installation IS NULL", (offset, window)
                    ).rowcount
                    if changed != 1:
                        raise ValueError("installed macro has no unique issued window")
                elif isinstance(record, ReceivedCall):
                    if source_record is None or source_record.evidence_profile != "native_runtime":
                        raise ValueError("native provider evidence has no declared runtime source profile")
                    UUID(record.attempt.attempt_id)
                    granted_window = index.execute(
                        "SELECT window FROM grants WHERE id=?", (record.attempt.attempt_id,)
                    ).fetchone()
                    if granted_window is None or granted_window[0] != record.window_id:
                        raise ValueError("received native call predates its source-owned grant")
                    previous = index.execute(
                        "SELECT window FROM attempts WHERE id=?", (record.attempt.attempt_id,)
                    ).fetchone()
                    if previous is not None and previous[0] != record.window_id:
                        raise ValueError("attempt identity was reused across private windows")
                    index.execute(
                        "INSERT INTO attempts VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET offset=excluded.offset",
                        (record.attempt.attempt_id, record.window_id, offset),
                    )
                elif isinstance(record, TeacherCall):
                    if source_record is None or source_record.evidence_profile != "scripted_teacher_logical_clock_v1":
                        raise ValueError("scripted teacher evidence has no declared source collector profile")
                    if index.execute("SELECT id FROM windows WHERE id=?", (str(record.window_id),)).fetchone() is None:
                        raise ValueError("scripted teacher has no issued private window")
                    index.execute(
                        "INSERT INTO attempts VALUES(?,?,?)", (str(record.attempt_id), str(record.window_id), offset)
                    )
                elif isinstance(record, EngineApplication):
                    if record.end_tick != record.start_tick + 1 or record.start_tick != last_tick + 1:
                        raise ValueError("physical engine applications must be consecutive single ticks")
                    last_tick = record.start_tick
                    if sorted(control.slot for control in record.controls) != [0, 1, 2, 3]:
                        raise ValueError("physical application must contain every unique assigned seat")
                    for control in record.controls:
                        if control.step != record.start_tick:
                            raise ValueError("control tick differs from actual engine application")
                        if control.directive_window_id is not None:
                            installed_window = index.execute(
                                "SELECT installation FROM windows WHERE id=?", (str(control.directive_window_id),)
                            ).fetchone()
                            if installed_window is None or installed_window[0] is None:
                                raise ValueError("physical engine control predates its installed macro")
                            index.execute(
                                "INSERT INTO controls VALUES(?,?,?,?,?)",
                                (
                                    str(control.directive_window_id),
                                    record.start_tick,
                                    record.actions[control.slot],
                                    record.vibe_actions[control.slot],
                                    control.cause,
                                ),
                            )
                elif isinstance(record, EngineOutcome):
                    if outcome is not None:
                        raise ValueError("private engine outcome repeated")
                    outcome = record
                elif isinstance(record, OwnershipState):
                    joined[record.scope] = record.joined
            if (
                source_record is None
                or policies is None
                or outcome is None
                or any(joined.get(scope) is not True for scope in ("engine", "players", "episode"))
            ):
                raise ValueError("canonical publication requires terminal engine and player ownership joins")
            unknown = index.execute(
                "SELECT window FROM attempts WHERE window NOT IN(SELECT id FROM windows) UNION "
                "SELECT window FROM controls WHERE window NOT IN(SELECT id FROM windows)"
            ).fetchone()
            if unknown is not None:
                raise ValueError("private received evidence references an unissued window")
            raw.seek(0)
            digest = hashlib.file_digest(raw, "sha256").hexdigest()
            identity = source_record.identity.model_dump(mode="json")
            common = {
                "episode_id": identity["episode_id"],
                "game": source_record.game,
                "game_version": identity["game_version"],
                "source_revision": identity["source_revision"],
                "image_digest": identity["image_digest"],
            }
            summary = EpisodeEvent.model_validate(
                {
                    **common,
                    "event_type": "episode",
                    "schema_version": "1",
                    "seed_family": source_record.seed_family,
                    "status": "completed" if outcome.reason == "complete" else "truncated",
                    "outcome": {
                        "results": outcome.results,
                        "runtime_configuration": source_record.runtime_configuration,
                        "evidence_profile": source_record.evidence_profile,
                        "physical_evidence_sha256": digest,
                        "physical_evidence_file": source.name,
                        "registered_policies": policies.model_dump(mode="json")["registrations"],
                        "slot_to_engine_seat": {str(slot): slot for slot in range(4)},
                        "ownership": joined,
                    },
                    "participant_outcomes": outcome.results["scores"],
                }
            )
            with tempfile.NamedTemporaryFile(dir=destination.parent, mode="w+b", delete=False) as output:
                try:
                    output.write(
                        b'{"schema_version":"1","episode":' + summary.model_dump_json().encode() + b',"decisions":['
                    )
                    for decision_index, (window_id, offset, installation) in enumerate(
                        index.execute("SELECT id,offset,installation FROM windows ORDER BY offset")
                    ):
                        raw.seek(offset)
                        issued = RECORD.validate_json(raw.readline(MAX_JOURNAL_LINE + 1))
                        assert isinstance(issued, PrivateWindow)
                        window = issued.window
                        registration = next(item for item in policies.registrations if item.slot == window.slot)
                        if not isinstance(registration, NativeRegistration) or registration.profile != window.profile:
                            raise ValueError("native window differs from its frozen assigned policy profile")
                        directive: InstalledMacro | None = None
                        if installation is not None:
                            raw.seek(installation)
                            installed = RECORD.validate_json(raw.readline(MAX_JOURNAL_LINE + 1))
                            assert isinstance(installed, InstalledMacro)
                            directive = installed
                            if (directive.directive.slot, directive.directive.turn) != (window.slot, window.turn):
                                raise ValueError("installed directive differs from assigned private window")
                        controls = list(
                            index.execute(
                                "SELECT tick,action,vibe,cause FROM controls WHERE window=? ORDER BY tick", (window_id,)
                            )
                        )
                        if controls and [row[0] for row in controls] != list(
                            range(controls[0][0], controls[-1][0] + 1)
                        ):
                            raise ValueError("macro engine applications are not one contiguous interval")
                        applied = bool(controls) and not any(row[3] for row in controls)
                        selected = None if directive is None else directive.selected_attempt_id
                        plan = None if directive is None else directive.directive.plan
                        attempts = []
                        for (attempt_offset,) in index.execute(
                            "SELECT offset FROM attempts WHERE window=? ORDER BY offset", (window_id,)
                        ):
                            raw.seek(attempt_offset)
                            received = RECORD.validate_json(raw.readline(MAX_JOURNAL_LINE + 1))
                            if isinstance(received, TeacherCall):
                                parsed = parse_plan(received.response, window.legal_stations, window.ally_aliases)
                                expected = decide(window)
                                is_selected = selected == received.attempt_id
                                if (
                                    received.response != expected.model_dump_json()
                                    or parsed != expected
                                    or (is_selected and parsed != plan)
                                ):
                                    raise ValueError(
                                        "scripted teacher differs from source-private decision or installed macro"
                                    )
                                attempts.append(
                                    DecisionAttempt(
                                        attempt_id=str(received.attempt_id),
                                        policy=TEACHER_POLICY,
                                        origin="teacher",
                                        inference_mode="text_action",
                                        prompt=[
                                            {"role": "system", "content": window.system},
                                            {"role": "user", "content": window.user},
                                        ],
                                        response=received.response,
                                        parsed_action=parsed.model_dump(mode="json"),
                                        accepted=is_selected and applied,
                                        rejection_reason=None
                                        if is_selected and applied
                                        else "not_selected_or_not_applied",
                                    )
                                )
                                continue
                            assert isinstance(received, ReceivedCall)
                            evidence = received.attempt
                            grant_row = index.execute(
                                "SELECT offset FROM grants WHERE id=? AND window=?", (evidence.attempt_id, window_id)
                            ).fetchone()
                            if grant_row is None:
                                raise ValueError("native attempt has no unique engine-issued request grant")
                            raw.seek(grant_row[0])
                            granted = RECORD.validate_json(raw.readline(MAX_JOURNAL_LINE + 1))
                            assert isinstance(granted, GrantedCall)
                            if (
                                granted.grant.window != window
                                or evidence.request != window.native_request(granted.grant.user)
                                or evidence.slot != window.slot
                            ):
                                raise ValueError("native attempt differs from exact source-owned request grant")
                            is_selected = selected is not None and evidence.attempt_id == str(selected)
                            parsed = None
                            if is_selected:
                                if (
                                    evidence.response_reader_joined is not True
                                    or evidence.response_complete is not True
                                    or not isinstance(evidence.raw_response, str)
                                ):
                                    raise ValueError("selected native attempt lacks actual completed reader ownership")
                                response = Response.model_validate_json(evidence.raw_response)
                                parsed = parse_plan(response.text, window.legal_stations, window.ally_aliases)
                                if response.text != evidence.response or parsed != plan or evidence.slot != window.slot:
                                    raise ValueError("selected native response differs from installed engine directive")
                            attempts.append(
                                model_attempt(evidence, "native-kitchen-macro-v1", is_selected and applied, parsed)
                            )
                        if selected is not None and not any(item.attempt_id == str(selected) for item in attempts):
                            raise ValueError("selected native attempt is absent from received evidence")
                        accepted = selected is not None and plan is not None and applied
                        event = DecisionEvent.model_validate(
                            {
                                **common,
                                "schema_version": "1",
                                "event_type": "decision",
                                "decision_id": window_id,
                                "decision_index": decision_index,
                                "seat": str(window.slot),
                                "visibility": "private",
                                "observation": window.observation,
                                "prompt": [
                                    {"role": "system", "content": window.system},
                                    {"role": "user", "content": window.user},
                                ],
                                "attempts": attempts,
                                "selected_attempt_id": None if selected is None else str(selected),
                                "executed_action": None
                                if plan is None or not controls
                                else plan.model_dump(mode="json"),
                                "execution": None
                                if not controls
                                else {
                                    "start_tick": controls[0][0],
                                    "end_tick": controls[-1][0] + 1,
                                    "tick_hz": 1
                                    / STEP_SECONDS.validate_python(source_record.runtime_configuration["step_seconds"]),
                                    "control_encoding": "u8-u8",
                                    "seat_controls_b64": base64.b64encode(
                                        bytes(value for row in controls for value in row[1:3])
                                    ).decode(),
                                },
                                "action_status": "accepted" if accepted else "fallback",
                                "fallback_origin": None if accepted else "engine_macro_not_applied",
                                "terminal": False,
                            }
                        )
                        output.write((b"," if decision_index else b"") + event.model_dump_json().encode())
                    output.write(b"]}\n")
                    output.flush()
                    os.fsync(output.fileno())
                    os.link(output.name, destination)
                finally:
                    os.unlink(output.name)


async def finalize_owned(source: Path, destination: Path) -> None:
    """Observe the actual finalizer process exit before admitting its candidate."""
    candidate = destination.with_name(destination.name + ".candidate.jsonl")
    child: asyncio.subprocess.Process | None = None
    exited: asyncio.Task | None = None
    abandoned = False
    deadline = asyncio.get_running_loop().time() + 30.0
    owner = lifecycle.current_owner.get()
    while owner is not None:
        if owner.stopping:
            raise lifecycle.OwnershipUnsettled("canonical finalization follows owner stop")
        if owner.deadline is not None:
            deadline = min(deadline, owner.deadline)
        owner = owner.parent
    stderr_path = destination.with_name(destination.name + ".finalizer.stderr")
    with os.fdopen(os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as errors:

        async def acquire() -> None:
            nonlocal child, exited
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "collab_cooking.coworld.trajectory",
                str(source),
                str(candidate),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=errors,
                close_fds=True,
                env=os.environ
                | {"PYTHONPATH": str(Path(__file__).parents[2]) + os.pathsep + os.environ.get("PYTHONPATH", "")},
            )
            if abandoned and child.returncode is None:
                child.kill()
            # The finalizer owns this wait. Ancestor cancellation must not turn
            # a cancelled wait coroutine into apparent OS-process completion.
            exited = asyncio.create_task(child.wait())

        creation = asyncio.create_task(acquire())
        try:
            async with asyncio.timeout_at(deadline):
                await asyncio.shield(creation)
            assert exited is not None
            if not await lifecycle.settle({exited}, deadline, cancel=False):
                raise TimeoutError("canonical finalizer exceeded its bounded source budget")
            if exited.result() != 0:
                raise RuntimeError("canonical finalizer failed; exact private error artifact retained")
        finally:
            abandoned = True
            cleanup = lifecycle.cleanup_deadline()
            if not await lifecycle.settle({creation}, cleanup, cancel=False):
                raise lifecycle.OwnershipUnsettled("canonical finalizer acquisition remains unresolved")
            creation.result()
            assert child is not None and exited is not None
            if child.returncode is None:
                child.kill()
            if not await lifecycle.settle({exited}, cleanup, cancel=False):
                raise lifecycle.OwnershipUnsettled("canonical finalizer writer did not join")
    os.link(candidate, destination)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise ValueError("canonical producer requires private source and candidate paths")
    finalize(Path(sys.argv[1]), Path(sys.argv[2]))

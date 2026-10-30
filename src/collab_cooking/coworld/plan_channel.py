"""Authenticated seat boundary for engine-issued controls and native call evidence."""

from __future__ import annotations

from typing import Annotated
from uuid import uuid4

from mettagrid.policy.policy_env_interface import PolicyEnvInterface
from pydantic import ConfigDict, Field, TypeAdapter

from .macro_executor import AdmittedControl, MacroExecutor
from .plan_admission import PlanAdmission, PlayerRoster
from .plan_protocol import (
    AttemptProgress,
    DirectiveInstalled,
    EvidenceReceived,
    ExternalRegistration,
    NativeRegistration,
    PhysicalAction,
    PhysicalObservation,
    PlanControl,
    PlanReply,
    Registration,
    ScriptedRegistration,
    Stop,
    Stopped,
)

PlayerFrame = Annotated[
    Registration | PhysicalAction | AttemptProgress | PlanReply | Stopped,
    Field(discriminator="type"),
]
PLAYER_FRAME = TypeAdapter(PlayerFrame, config=ConfigDict(hide_input_in_errors=True))


class PlanChannel:
    """Player packets cannot choose a seat, installed control, or stop identity."""

    def __init__(
        self,
        slot: int,
        roster: PlayerRoster,
        admission: PlanAdmission,
        pei: PolicyEnvInterface,
        layout: str,
        fallback_baseline: str,
    ):
        self.slot = slot
        self.layout = layout
        self.action_names = set(pei.action_names)
        self.external_pending: PhysicalObservation | None = None
        self.roster = roster
        self.admission = admission
        self.executor = MacroExecutor(pei, slot, layout, fallback_baseline)
        self.submitted: PhysicalAction | None = None
        self.stop_request: Stop | None = None
        self.acknowledged = False

    async def receive(self, frame: PlayerFrame) -> PlanControl | None:
        if isinstance(frame, (NativeRegistration, ScriptedRegistration, ExternalRegistration)):
            if self.stop_request is not None:
                raise ValueError("registration follows engine stop")
            self.roster.register(self.slot, frame)
            if isinstance(frame, ScriptedRegistration):
                self.executor = MacroExecutor(self.executor.pei, self.slot, self.layout, frame.baseline)
            return None
        if isinstance(frame, PhysicalAction):
            if self.stop_request is not None:
                raise ValueError("physical action follows engine stop")
            registration = self.roster.player(self.slot)
            pending = self.external_pending if isinstance(registration, ExternalRegistration) else self.executor.pending
            if pending is None or frame.slot != self.slot or frame.request_id != pending.request_id:
                raise ValueError("physical action differs from current private window")
            if self.submitted is not None:
                raise ValueError("physical action repeats current private window")
            self.submitted = frame.model_copy(deep=True)
            return None
        if isinstance(frame, AttemptProgress):
            if self.acknowledged:
                raise ValueError("native progress follows acknowledged engine stop")
            self.admission.progress(self.slot, frame)
            return None
        if isinstance(frame, PlanReply):
            return await self.admission.reply(self.slot, frame)
        if self.stop_request is None or frame.request_id != self.stop_request.request_id:
            raise ValueError("stopped acknowledgement has no matching engine stop")
        if self.acknowledged:
            raise ValueError("stopped acknowledgement repeats engine stop")
        attempts = [
            attempt
            for entry in self.admission.history.values()
            if entry.window.slot == self.slot
            for attempt in entry.attempts.values()
        ]
        if not frame.joined or any(
            attempt.response_reader_joined is False
            or attempt.latency_ms is None
            or (attempt.http_status is not None and attempt.response_reader_joined is not True)
            for attempt in attempts
        ):
            raise ValueError("stopped acknowledgement has unresolved native ownership")
        self.acknowledged = True
        return None

    def observe(self, observation: list[list[int]], step: int) -> PhysicalObservation:
        if self.stop_request is not None:
            raise ValueError("private observation follows engine stop")
        registration = self.roster.player(self.slot)
        message = PhysicalObservation(request_id=uuid4(), slot=self.slot, step=step, observation=observation)
        if isinstance(registration, ExternalRegistration):
            if self.external_pending is not None:
                raise ValueError("external observation overlaps its predecessor")
            self.external_pending = message.model_copy(deep=True)
        else:
            self.executor.issue(message)
        return message

    def admit(self, fallback: str) -> AdmittedControl:
        if self.stop_request is not None:
            raise ValueError("physical admission follows engine stop")
        if isinstance(self.roster.player(self.slot), ExternalRegistration):
            if self.external_pending is None:
                raise ValueError("external action has no issued private observation")
            if self.submitted is None and not fallback:
                raise ValueError("missing external action requires engine-observed fallback")
            cause = (
                fallback
                if self.submitted is None
                else ""
                if self.submitted.action_name in self.action_names
                else "invalid_action"
            )
            result = AdmittedControl(
                request_id=self.external_pending.request_id,
                slot=self.slot,
                step=self.external_pending.step,
                directive_window_id=None,
                expected_action=None,
                submitted_action=None if self.submitted is None else self.submitted.action_name,
                installed_action=self.submitted.action_name if not cause and self.submitted is not None else "noop",
                cause=cause,
            )
            self.external_pending = None
        else:
            result = self.executor.admit(self.submitted, fallback=fallback if self.submitted is None else "")
        self.submitted = None
        return result

    def install(self, message: DirectiveInstalled) -> None:
        if self.stop_request is not None:
            raise ValueError("directive installation follows engine stop")
        if isinstance(self.roster.player(self.slot), ExternalRegistration):
            raise ValueError("external research controls cannot install native macro directives")
        self.executor.install(message)

    def stop(self) -> Stop:
        if self.stop_request is None:
            self.stop_request = Stop(request_id=uuid4())
        return self.stop_request

    def evidence_received(self) -> EvidenceReceived:
        if not self.acknowledged or self.stop_request is None:
            raise ValueError("evidence credit requires matching joined engine stop")
        return EvidenceReceived(request_id=self.stop_request.request_id)

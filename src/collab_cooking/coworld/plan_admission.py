"""Engine-owned simultaneous windows, shared rate grants, and reply authority."""

from __future__ import annotations

import asyncio
import base64
import math
import time
from collections import deque
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from .attempt_store import AttemptStore
from .native import Response, TemperedSamplingEvidence
from .plan_protocol import AttemptProgress, PlanControl, PlanReply, PlanWindow, Registration, RequestGrant
from .plans import RETRY_HINT, PlanError, parse_plan


@dataclass
class AdmittedWindow:
    window: PlanWindow
    deadline: float
    grants: list[RequestGrant] = field(default_factory=list)
    attempts: AttemptStore = field(default_factory=AttemptStore)
    closed: bool = False
    parsed_plan: PlanControl | None = None
    selected_attempt_id: UUID | None = None
    rejection_reason: str | None = None


class PlayerRoster:
    """Freeze authenticated-seat policy inputs before issuing any private observation."""

    def __init__(self):
        self.registrations: dict[int, Registration] = {}
        self.frozen = False

    def register(self, assigned_slot: int, registration: Registration) -> None:
        if registration.slot != assigned_slot:
            raise ValueError("policy registration differs from its authenticated seat")
        if self.frozen or assigned_slot in self.registrations:
            raise ValueError("policy registration is closed for this seat")
        self.registrations[assigned_slot] = registration.model_copy(deep=True)

    def freeze(self) -> None:
        if self.frozen:
            raise ValueError("policy roster is already frozen")
        if set(self.registrations) != {0, 1, 2, 3}:
            raise ValueError("policy roster requires all four authenticated seats")
        self.frozen = True

    def player(self, slot: int) -> Registration:
        if not self.frozen:
            raise ValueError("private observations require a frozen policy roster")
        return self.registrations[slot].model_copy(deep=True)


class PlanAdmission:
    """No inference transport lives in the game: every call consumes a game grant."""

    def __init__(self, max_requests_per_minute: int):
        if max_requests_per_minute <= 0:
            raise ValueError("plan rate budget must be positive")
        self.max_requests_per_minute = max_requests_per_minute
        self.events: deque[float] = deque()
        self.windows: dict[int, AdmittedWindow] = {}
        self.history: dict[UUID, AdmittedWindow] = {}
        self.closed = False
        self.sealed = False

    def begin(self, windows: list[PlanWindow], timeout_seconds: float) -> list[RequestGrant]:
        """All seats share one boundary/deadline and the same rolling call budget."""
        if self.closed or any(not entry.closed for entry in self.windows.values()):
            raise ValueError("plan batch admission is not available")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60:
            raise ValueError("plan window requires a finite bounded deadline")
        if len({window.window_id for window in windows}) != len(windows):
            raise ValueError("plan batch repeats a window identity")
        if any(window.window_id in self.history for window in windows):
            raise ValueError("plan batch reuses a prior window identity")
        if len({window.slot for window in windows}) != len(windows):
            raise ValueError("plan batch repeats a seat")
        now = time.monotonic()
        self.windows = {
            window.slot: AdmittedWindow(window.model_copy(deep=True), now + timeout_seconds) for window in windows
        }
        self.history.update({entry.window.window_id: entry for entry in self.windows.values()})
        grants = []
        for entry in self.windows.values():
            grant = self.grant(entry.window.slot, now=now)
            if grant is not None:
                grants.append(grant)
        return grants

    def grant(self, slot: int, *, now: float | None = None) -> RequestGrant | None:
        stamp = time.monotonic() if now is None else now
        entry = self.windows[slot]
        if self.closed or entry.closed or entry.deadline <= stamp:
            raise ValueError("plan grant belongs to a closed window")
        if len(entry.grants) >= 2:
            raise ValueError("plan retry budget exhausted")
        while self.events and stamp - self.events[0] >= 60:
            self.events.popleft()
        if len(self.events) >= self.max_requests_per_minute:
            entry.closed = True
            entry.rejection_reason = "rate_budget"
            return None
        index = len(entry.grants)
        if index and entry.rejection_reason is None:
            raise ValueError("plan retry requires a source-observed rejection")
        user = entry.window.user if index == 0 else f"{entry.window.user}\n\n{RETRY_HINT}"
        grant = RequestGrant(
            window=entry.window.model_copy(deep=True),
            attempt_id=uuid4(),
            attempt_index=index,
            user=user,
            timeout_seconds=min(60, entry.deadline - stamp),
        )
        entry.grants.append(grant)
        self.events.append(stamp)
        return grant.model_copy(deep=True)

    def progress(self, slot: int, message: AttemptProgress) -> None:
        if message.window_id not in self.history:
            raise ValueError("native progress has no issued private window")
        entry = self.history[message.window_id]
        if self.sealed or entry.parsed_plan is not None or entry.window.slot != slot:
            raise ValueError("progress belongs to an inactive private window")
        matching = [grant for grant in entry.grants if grant.attempt_id == message.attempt_id]
        if len(matching) != 1:
            raise ValueError("native progress has no issued call grant")
        grant = matching[0]
        attempt = message.evidence
        if (
            attempt.attempt_id != str(grant.attempt_id)
            or attempt.slot != slot
            or attempt.request != entry.window.native_request(grant.user)
            or attempt.timeout_ms != grant.timeout_seconds * 1000
        ):
            raise ValueError("native progress rewrites its engine-issued request")
        if attempt.response_body_b64 is not None:
            base64.b64decode(attempt.response_body_b64, validate=True)
        prior = entry.attempts.get(message.attempt_id)
        if prior is not None:
            if prior.response_reader_joined is True and attempt != prior:
                raise ValueError("native progress rewrites joined response evidence")
            if prior.endpoint is not None and attempt.endpoint != prior.endpoint:
                raise ValueError("native progress rewrites original endpoint")
            if prior.http_status is not None and attempt.http_status != prior.http_status:
                raise ValueError("native progress rewrites observed HTTP status")
            if prior.received_header_pairs and attempt.received_header_pairs != prior.received_header_pairs:
                raise ValueError("native progress rewrites observed header pairs")
            for name in (
                "platform_call_id",
                "provider_request_id",
                "model_identity",
                "tokenizer_identity",
                "chat_template_sha256",
                "model",
                "usage",
                "sampling_evidence",
                "stop_reason",
                "response",
            ):
                observed = getattr(prior, name)
                if observed is not None and getattr(attempt, name) != observed:
                    raise ValueError("native progress rewrites observed response metadata")
            if prior.response_headers and prior.response_headers != attempt.response_headers:
                raise ValueError("native progress rewrites observed normalized headers")
            if prior.response_body_b64 is not None:
                if attempt.response_body_b64 is None or not base64.b64decode(
                    attempt.response_body_b64, validate=True
                ).startswith(base64.b64decode(prior.response_body_b64, validate=True)):
                    raise ValueError("native progress rewrites received byte prefix")
            if prior.response_complete is True and (
                attempt.response_complete is not True or attempt.response_body_b64 != prior.response_body_b64
            ):
                raise ValueError("native progress rewrites complete response bytes")
        entry.attempts[message.attempt_id] = attempt.model_copy(deep=True)

    async def reply(self, slot: int, message: PlanReply) -> PlanControl | None:
        """Parse the actual received text independently of the separately submitted plan."""
        if message.window_id not in self.history:
            raise ValueError("plan reply has no issued private window")
        entry = self.history[message.window_id]
        if self.closed or entry.closed or entry.window.slot != slot:
            raise ValueError("plan reply belongs to an inactive private window")
        if time.monotonic() >= entry.deadline:
            entry.closed = True
            entry.rejection_reason = "timeout"
            return None
        if message.attempt_id not in entry.attempts:
            raise ValueError("plan reply has no observed granted call")
        attempt = entry.attempts[message.attempt_id]
        if (
            not message.reader_joined
            or attempt.response_reader_joined is False
            or (attempt.http_status is not None and attempt.response_reader_joined is not True)
        ):
            raise ValueError("plan reply has an unresolved native reader")
        if attempt.response_complete is not True or attempt.http_status != 200:
            entry.rejection_reason = "transport"
            return None
        if attempt.raw_response is None or attempt.response_body_b64 is None:
            raise ValueError("completed native plan lacks received wire evidence")
        raw = base64.b64decode(attempt.response_body_b64, validate=True).decode("utf-8")
        if raw != attempt.raw_response:
            raise ValueError("native received bytes differ from supplied raw response")
        received = Response.model_validate_json(raw)
        if received.text != attempt.response or message.response != received.text:
            raise ValueError("native text differs from received response")
        stop_reason = (
            received.stop_reason if received.sampling_evidence is None else received.sampling_evidence.stop_reason
        )
        if (
            attempt.model != received.model
            or attempt.usage != received.usage
            or attempt.sampling_evidence != received.sampling_evidence
            or attempt.stop_reason != stop_reason
        ):
            raise ValueError("native response metadata differs from its received body")
        sample = received.sampling_evidence
        if sample is not None:
            temperature = (
                sample.temperature
                if isinstance(sample, TemperedSamplingEvidence)
                else 0
                if sample.sampling is None
                else 1
            )
            if (
                entry.window.profile.temperature != temperature
                or entry.window.profile.top_p != 1
                or sample.max_new_tokens != entry.window.profile.max_tokens
                or sample.response != received.text
                or received.usage.input_tokens != len(sample.prompt_token_ids)
                or received.usage.output_tokens != len(sample.completion_token_ids)
                or received.stop_reason != ("end_turn" if sample.stop_reason == "eos" else "max_tokens")
            ):
                raise ValueError("native sampled response differs from its issued decoder or completion")

        async def parse() -> PlanControl:
            return parse_plan(received.text, entry.window.legal_stations, entry.window.ally_aliases)

        # Parsing is source-owned and synchronous; retain a typed terminal task result
        # instead of masking parser errors or trusting a player's rejection string.
        task = asyncio.create_task(parse())
        await asyncio.wait({task})
        error = task.exception()
        if isinstance(error, PlanError):
            entry.rejection_reason = error.cause
            return None
        if error is not None:
            raise error
        parsed = task.result()
        if parsed != message.submitted_plan or message.rejection_reason is not None:
            raise ValueError("submitted plan differs from independently parsed native text")
        entry.parsed_plan = parsed
        entry.selected_attempt_id = message.attempt_id
        entry.closed = True
        return parsed

    def stop(self) -> None:
        """Close new calls/actions; admitted owners may still send final received facts."""
        self.closed = True
        for entry in self.history.values():
            entry.closed = True

    def seal(self) -> None:
        """The episode owner calls this only after actual player/writer joins."""
        if not self.closed:
            raise ValueError("private plan evidence cannot seal before stopping admission")
        if any(
            attempt.response_reader_joined is False
            for entry in self.history.values()
            for attempt in entry.attempts.values()
        ):
            raise ValueError("private plan evidence has an explicitly unresolved reader")
        self.sealed = True
        for entry in self.history.values():
            entry.attempts.close_joined()

"""Source-owned private-view teaching through the ordinary game and controller.

This explicit collection profile advances the authored logical clock without
wall-clock sleeps. It acquires no native inference or platform receipts.
"""

from __future__ import annotations

from collections.abc import Mapping
from fractions import Fraction
from typing import Any
from uuid import uuid4

from .live_episode import LiveMettaGridEpisode
from .macro_executor import MacroExecutor
from .plan_protocol import (
    DirectiveInstalled,
    EvidenceReceived,
    NativeProfile,
    NativeRegistration,
    PhysicalAction,
    PhysicalObservation,
    PlayerConfiguration,
    Stop,
    Stopped,
)
from .plans import parse_plan
from .private_journal import PrivateWindow, TeacherCall
from .teacher import decide

TEACHER_PROMPT = "Cook using only your ordinary private kitchen observation."


class TeacherEpisode(LiveMettaGridEpisode):
    """The original physics loop, with immediate scripted macro decisions."""

    async def wait_shutdown_grace(self) -> None:
        # This collector exposes no replay HTTP service. Its original authored
        # grace remains recorded; no wall-clock runtime qualification is claimed.
        return

    async def _plan_boundary(self, step: int) -> None:
        if step % self.config.plan_interval_steps:
            return
        logical_now = Fraction(str(self.config.step_seconds)) * int(self.sim.current_step)
        if self.last_batch_at and logical_now - Fraction(str(self.last_batch_at)) < Fraction(
            str(self.config.min_plan_interval_seconds)
        ):
            return
        seats = self._prompt_seats()
        if not seats:
            return
        self.turn += 1
        self.last_batch_at = float(logical_now)
        # Freeze every simultaneous private window before any new note/radio update.
        windows = [self.private_window(seat.slot, NativeProfile()) for seat in seats]
        assert self.journal is not None
        for window in windows:
            self.journal.append(PrivateWindow(window=window))
        for window in windows:
            response = decide(window).model_dump_json()
            parsed = parse_plan(response, window.legal_stations, window.ally_aliases)
            attempt_id = uuid4()
            self.journal.append(TeacherCall(window_id=window.window_id, attempt_id=attempt_id, response=response))
            installed = DirectiveInstalled(
                window_id=window.window_id, slot=window.slot, turn=window.turn, plan=parsed, cause=""
            )
            self.install_macro(installed, attempt_id, origin="teacher")
            await self._send_to_slot(window.slot, installed.model_dump(mode="json"))


class TeacherSocket:
    """An explicit in-process scripted actor, never a hosted/native model proof."""

    def __init__(self, episode: TeacherEpisode):
        self.episode = episode
        self.connection_id = ""
        self.configuration: PlayerConfiguration | None = None
        self.controller: MacroExecutor | None = None
        self.finished = False
        self.closed = False

    async def register(self) -> None:
        assert self.configuration is not None
        configuration = self.configuration
        self.controller = MacroExecutor(configuration.policy_env, configuration.slot, configuration.layout, "brigade")
        registration = NativeRegistration(slot=configuration.slot, prompt=TEACHER_PROMPT, profile=NativeProfile())
        await self.episode.handle_player_message(self.connection_id, registration.model_dump(mode="json"))

    async def send_json(self, data: Mapping[str, Any]) -> None:
        value = data
        kind = value["type"]
        if kind == "player_config":
            self.configuration = PlayerConfiguration.model_validate(value)
            return
        assert self.controller is not None
        if kind == "observation":
            observation = PhysicalObservation.model_validate(value)
            self.controller.issue(observation)
            action = PhysicalAction(
                request_id=observation.request_id, slot=observation.slot, action_name=self.controller.expected
            )
            if self.controller.admit(action).cause:
                raise ValueError("source teacher physical actor differs from its ordinary controller")
            await self.episode.handle_player_message(self.connection_id, action.model_dump(mode="json"))
        elif kind == "plan_installed":
            self.controller.install(DirectiveInstalled.model_validate(value))
        elif kind == "stop":
            stop = Stop.model_validate(value)
            if self.controller.pending is not None:
                raise ValueError("source teacher actor has an unresolved physical observation")
            await self.episode.handle_player_message(
                self.connection_id, Stopped(request_id=stop.request_id, joined=True).model_dump(mode="json")
            )
        elif kind == "evidence_received":
            EvidenceReceived.model_validate(value)
            self.finished = True
        elif kind != "final":
            raise ValueError("source teacher actor received an undeclared inference frame")

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = True


async def seat_teachers(episode: TeacherEpisode) -> list[TeacherSocket]:
    sockets = []
    for slot in range(4):
        actor = TeacherSocket(episode)
        actor.connection_id = await episode.connect_player(slot, actor)
        await actor.register()
        sockets.append(actor)
    return sockets

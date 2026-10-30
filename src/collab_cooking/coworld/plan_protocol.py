"""Game-issued private plan windows and per-player native request grants."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from mettagrid.policy.policy_env_interface import PolicyEnvInterface
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, StrictInt, TypeAdapter

from .native import Attempt, Request

PLAYER_PROTOCOL = "collab-cooking.player.v2"
MAX_FRAME_BYTES = 16 * 1024 * 1024
DEFAULT_MODEL = "anthropic/claude-haiku-4.5"


class PrivateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True, allow_inf_nan=False)


class NativeProfile(PrivateModel):
    """Policy-owned inputs frozen by the engine before its first plan window."""

    model: str = Field(default=DEFAULT_MODEL, min_length=1, max_length=256)
    max_tokens: StrictInt = Field(default=900, gt=0, le=4096)
    temperature: float = Field(default=1.0, strict=True, ge=0, le=1)
    top_p: StrictInt = Field(default=1, ge=1, le=1)


class NativeRegistration(PrivateModel):
    """Recorded policy inputs, never a checkpoint identity or teacher authority."""

    type: Literal["register"] = "register"
    protocol: Literal["collab-cooking.player.v2"] = PLAYER_PROTOCOL
    kind: Literal["native"] = "native"
    slot: StrictInt = Field(ge=0, le=3)
    prompt: str = Field(min_length=1, max_length=1200)
    profile: NativeProfile


class ScriptedRegistration(PrivateModel):
    type: Literal["register"] = "register"
    protocol: Literal["collab-cooking.player.v2"] = PLAYER_PROTOCOL
    kind: Literal["scripted"] = "scripted"
    slot: StrictInt = Field(ge=0, le=3)
    baseline: Literal["brigade", "runner", "passer", "courier"]


class ExternalRegistration(PrivateModel):
    """Independent per-tick research controls, never native macro or teacher evidence."""

    type: Literal["register"] = "register"
    protocol: Literal["collab-cooking.player.v2"] = PLAYER_PROTOCOL
    kind: Literal["external"] = "external"
    slot: StrictInt = Field(ge=0, le=3)


Registration = Annotated[NativeRegistration | ScriptedRegistration | ExternalRegistration, Field(discriminator="kind")]


class PhysicalObservation(PrivateModel):
    """One exact engine-issued private tick window for the ordinary executor."""

    type: Literal["observation"] = "observation"
    protocol: Literal["collab-cooking.player.v2"] = PLAYER_PROTOCOL
    request_id: UUID
    slot: StrictInt = Field(ge=0, le=3)
    step: StrictInt = Field(ge=0)
    observation: list[Annotated[list[StrictInt], Field(min_length=3, max_length=3)]]


class PhysicalAction(PrivateModel):
    """A separately submitted physical control, admitted against its issued UUID."""

    type: Literal["action"] = "action"
    protocol: Literal["collab-cooking.player.v2"] = PLAYER_PROTOCOL
    request_id: UUID
    slot: StrictInt = Field(ge=0, le=3)
    action_name: str = Field(min_length=1, max_length=64)


class PlanWindow(PrivateModel):
    """Exact private prompt and legality context issued at the shared turn boundary."""

    window_id: UUID
    slot: StrictInt = Field(ge=0, le=3)
    turn: StrictInt = Field(gt=0)
    step: StrictInt = Field(ge=0)
    alias: str
    system: str
    user: str
    legal_stations: list[str] = Field(min_length=1, max_length=10)
    ally_aliases: list[str] = Field(min_length=1, max_length=4)
    observation: dict[str, JsonValue]
    profile: NativeProfile

    def native_request(self, user: str) -> Request:
        return Request(
            model=self.profile.model,
            max_tokens=self.profile.max_tokens,
            temperature=self.profile.temperature,
            top_p=self.profile.top_p,
            system=self.system,
            messages=[{"role": "user", "content": user}],
        )


class RequestGrant(PrivateModel):
    """One source-owned rate/retry admission, never a serving identity claim."""

    type: Literal["plan_grant"] = "plan_grant"
    protocol: Literal["collab-cooking.player.v2"] = PLAYER_PROTOCOL
    window: PlanWindow
    attempt_id: UUID
    attempt_index: StrictInt = Field(ge=0, le=1)
    user: str
    timeout_seconds: float = Field(gt=0, le=60)


class AttemptProgress(PrivateModel):
    type: Literal["plan_progress"] = "plan_progress"
    protocol: Literal["collab-cooking.player.v2"] = PLAYER_PROTOCOL
    window_id: UUID
    attempt_id: UUID
    evidence: Attempt


class PlanControl(PrivateModel):
    """The parser-normalized directive submitted separately from received text."""

    station: str = Field(min_length=1, max_length=12)
    recipe: Literal["salad", "soup", "fries", "any"]
    zone: Literal["left", "right", "pass", "any"]
    handoff: str = Field(min_length=1, max_length=8)
    yield_to: str = Field(min_length=1, max_length=8)
    say: str = Field(max_length=120)
    note: str = Field(max_length=200)

    def replay_event(self, slot: int, alias: str, turn: int, src: str) -> dict[str, JsonValue]:
        """Explicitly public directive projection excludes the private note."""
        return {
            "ev": "plan",
            "slot": slot,
            "alias": alias,
            "turn": turn,
            "station": self.station,
            "recipe": self.recipe,
            "zone": self.zone,
            "handoff": self.handoff,
            "yield_to": self.yield_to,
            "say": self.say,
            "src": src,
        }


class DirectiveInstalled(PrivateModel):
    """Only the game installs a parser-verified directive in the ordinary executor."""

    type: Literal["plan_installed"] = "plan_installed"
    protocol: Literal["collab-cooking.player.v2"] = PLAYER_PROTOCOL
    window_id: UUID
    slot: StrictInt = Field(ge=0, le=3)
    turn: StrictInt = Field(gt=0)
    plan: PlanControl | None
    cause: str = Field(max_length=64)


class PlanReply(PrivateModel):
    type: Literal["plan_reply"] = "plan_reply"
    protocol: Literal["collab-cooking.player.v2"] = PLAYER_PROTOCOL
    window_id: UUID
    attempt_id: UUID
    response: str | None
    submitted_plan: PlanControl | None
    rejection_reason: str | None
    reader_joined: StrictBool


class Stop(PrivateModel):
    type: Literal["stop"] = "stop"
    protocol: Literal["collab-cooking.player.v2"] = PLAYER_PROTOCOL
    request_id: UUID


class Stopped(PrivateModel):
    type: Literal["stopped"] = "stopped"
    protocol: Literal["collab-cooking.player.v2"] = PLAYER_PROTOCOL
    request_id: UUID
    joined: StrictBool


class EvidenceReceived(PrivateModel):
    type: Literal["evidence_received"] = "evidence_received"
    protocol: Literal["collab-cooking.player.v2"] = PLAYER_PROTOCOL
    request_id: UUID


class PlayerConfiguration(PrivateModel):
    type: Literal["player_config"] = "player_config"
    protocol: Literal["collab-cooking.player.v2"] = PLAYER_PROTOCOL
    slot: StrictInt = Field(ge=0, le=3)
    alias: Literal["Cog-A", "Cog-B", "Cog-C", "Cog-D"]
    layout: str
    policy_env: PolicyEnvInterface


GameFrame = Annotated[
    PlayerConfiguration | PhysicalObservation | RequestGrant | DirectiveInstalled | Stop | EvidenceReceived,
    Field(discriminator="type"),
]
GAME_FRAME = TypeAdapter(GameFrame, config=ConfigDict(hide_input_in_errors=True))

"""Private source evidence stays writable until every acquired episode owner joins."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit
from uuid import UUID

from pydantic import Field, JsonValue, StrictBool, StrictInt, TypeAdapter

from .macro_executor import AdmittedControl
from .native import Attempt
from .plan_protocol import DirectiveInstalled, PhysicalObservation, PlanWindow, PrivateModel, Registration, RequestGrant


class SourceIdentity(PrivateModel):
    episode_id: str = Field(min_length=1)
    # The runner may supply a source locator or local image tag, not a commit pin.
    source_revision: str | None = Field(default=None, min_length=1)
    game_version: str | None = Field(default=None, min_length=1)
    image_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class SourceRecord(PrivateModel):
    kind: Literal["source"] = "source"
    protocol: Literal["collab-cooking.private-evidence.v1"] = "collab-cooking.private-evidence.v1"
    game: Literal["collab_cooking"] = "collab_cooking"
    seed_family: str
    identity: SourceIdentity
    runtime_configuration: dict[str, JsonValue]
    evidence_profile: Literal["native_runtime", "scripted_teacher_logical_clock_v1"] = "native_runtime"


class RegisteredPolicies(PrivateModel):
    kind: Literal["registered_policies"] = "registered_policies"
    registrations: list[Registration] = Field(min_length=4, max_length=4)


class OwnershipState(PrivateModel):
    kind: Literal["ownership"] = "ownership"
    joined: StrictBool
    scope: Literal["players", "engine", "episode"] = "episode"
    reason: str


class PrivateTick(PrivateModel):
    kind: Literal["private_tick"] = "private_tick"
    window: PhysicalObservation


class PrivateWindow(PrivateModel):
    kind: Literal["private_plan"] = "private_plan"
    window: PlanWindow


class GrantedCall(PrivateModel):
    kind: Literal["granted_call"] = "granted_call"
    grant: RequestGrant


class ReceivedCall(PrivateModel):
    kind: Literal["received_call"] = "received_call"
    window_id: str
    attempt: Attempt


class TeacherCall(PrivateModel):
    """Only the source collector writes these; player ingress has no teacher frame."""

    kind: Literal["teacher_call"] = "teacher_call"
    window_id: UUID
    attempt_id: UUID
    response: str = Field(min_length=1, max_length=4096)


class InstalledMacro(PrivateModel):
    kind: Literal["installed_macro"] = "installed_macro"
    directive: DirectiveInstalled
    selected_attempt_id: UUID | None = None


class EngineApplication(PrivateModel):
    kind: Literal["engine_application"] = "engine_application"
    start_tick: StrictInt = Field(ge=0)
    end_tick: StrictInt = Field(ge=0)
    controls: list[AdmittedControl] = Field(min_length=4, max_length=4)
    actions: list[StrictInt] = Field(min_length=4, max_length=4)
    vibe_actions: list[StrictInt] = Field(min_length=4, max_length=4)
    effects: list[dict[str, JsonValue]]
    scores: list[float]


class EngineOutcome(PrivateModel):
    kind: Literal["engine_outcome"] = "engine_outcome"
    reason: str
    results: dict[str, JsonValue]


Record = Annotated[
    SourceRecord
    | RegisteredPolicies
    | OwnershipState
    | PrivateTick
    | PrivateWindow
    | GrantedCall
    | ReceivedCall
    | TeacherCall
    | InstalledMacro
    | EngineApplication
    | EngineOutcome,
    Field(discriminator="kind"),
]
RECORD = TypeAdapter(Record)


class Journal:
    """Append-only owner-only spool; unresolved owners never close its resources."""

    def __init__(
        self,
        path: Path,
        identity: SourceIdentity,
        configuration: dict[str, JsonValue],
        *,
        evidence_profile: Literal["native_runtime", "scripted_teacher_logical_clock_v1"] = "native_runtime",
    ):
        self.path = path
        suffix = ".evidence.partial.jsonl"
        self.canonical_path = (
            Path(str(path)[: -len(suffix)]) if str(path).endswith(suffix) else path.with_suffix(".complete.jsonl")
        )
        self.identity = identity
        self.closed = False
        path.parent.mkdir(parents=True, exist_ok=True)
        source = SourceRecord(
            identity=identity,
            seed_family=f"collab_cooking:{configuration['seed']}",
            runtime_configuration=configuration,
            evidence_profile=evidence_profile,
        )
        self.stream = os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb")
        self.append(source)

    @classmethod
    def from_environment(
        cls,
        configuration: dict[str, JsonValue],
        *,
        evidence_profile: Literal["native_runtime", "scripted_teacher_logical_clock_v1"] = "native_runtime",
    ) -> Journal | None:
        if "COGAME_SAVE_TRAJECTORY_URI" not in os.environ:
            return None
        uri = urlsplit(os.environ["COGAME_SAVE_TRAJECTORY_URI"])
        if uri.scheme != "file" or uri.netloc or uri.query or uri.fragment or not Path(unquote(uri.path)).is_absolute():
            raise ValueError("private trajectory requires an absolute file URI")
        identity = SourceIdentity(
            episode_id=os.environ["COWORLD_EPISODE_ID"],
            source_revision=os.environ.get("COWORLD_SOURCE_REVISION"),
            game_version=os.environ.get("COWORLD_GAME_VERSION"),
            image_digest=os.environ.get("COWORLD_GAME_IMAGE_DIGEST"),
        )
        # This is raw private evidence, not a CompleteEpisode or approved training corpus.
        return cls(
            Path(unquote(uri.path) + ".evidence.partial.jsonl"),
            identity,
            configuration,
            evidence_profile=evidence_profile,
        )

    def append(self, record: Record) -> None:
        if self.closed:
            raise ValueError("private evidence writer is already joined")
        self.stream.write(RECORD.dump_json(record) + b"\n")
        self.stream.flush()

    def close_joined(self) -> None:
        self.stream.flush()
        self.stream.close()
        self.closed = True

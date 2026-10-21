"""Source-owned verification of the shipped executor on exact private tick windows."""

from uuid import UUID

from mettagrid.policy.policy_env_interface import PolicyEnvInterface
from pydantic import Field, StrictInt

from collab_cooking.agent.brain.policy import KitchenBrain, PlanDirective

from .observation import decode_observation
from .plan_protocol import DirectiveInstalled, PhysicalAction, PhysicalObservation, PrivateModel


class AdmittedControl(PrivateModel):
    """Admission facts; actual engine application is a separate game-owned event."""

    request_id: UUID
    slot: StrictInt = Field(ge=0, le=3)
    step: StrictInt = Field(ge=0)
    directive_window_id: UUID | None
    expected_action: str | None
    submitted_action: str | None
    installed_action: str
    cause: str


class MacroExecutor:
    """Mirror once per issued observation, never step or inspect the actual simulator."""

    def __init__(self, pei: PolicyEnvInterface, slot: int, layout: str, baseline: str):
        self.slot = slot
        self.pei = pei
        self.brain = KitchenBrain(pei, slot, layout=layout, baseline=baseline)
        self.state = self.brain.initial_agent_state()
        self.directive: DirectiveInstalled | None = None
        self.pending: PhysicalObservation | None = None
        self.expected = "noop"
        self.last_step = -1

    def install(self, message: DirectiveInstalled) -> None:
        if self.pending is not None:
            raise ValueError("directive installation overlaps an unresolved tick window")
        if message.slot != self.slot:
            raise ValueError("directive installation differs from assigned seat")
        if self.directive is not None and message.turn <= self.directive.turn:
            raise ValueError("directive installation repeats or rewinds its turn")
        self.directive = message.model_copy(deep=True)
        plan = message.plan
        self.brain.apply_plan(
            self.state,
            None
            if plan is None
            else PlanDirective(
                turn=message.turn,
                station=plan.station,
                recipe=plan.recipe,
                zone=plan.zone,
                handoff=plan.handoff,
                yield_to=plan.yield_to,
                say=plan.say,
                src="llm",
            ),
        )

    def issue(self, message: PhysicalObservation) -> None:
        if self.pending is not None:
            raise ValueError("physical observation overlaps its predecessor")
        if message.slot != self.slot or message.step != self.last_step + 1:
            raise ValueError("physical observation differs from assigned seat or next engine tick")
        self.pending = message.model_copy(deep=True)
        observation = decode_observation(self.pei, self.pending.observation, self.slot)
        action, self.state = self.brain.step_with_state(observation, self.state)
        self.expected = action.name
        self.last_step = message.step

    def admit(self, message: PhysicalAction | None, *, fallback: str = "") -> AdmittedControl:
        """Reject rewritten controls without claiming they reached the engine."""
        if self.pending is None:
            raise ValueError("physical action has no issued private observation")
        issued = self.pending
        if message is not None and (message.slot != self.slot or message.request_id != issued.request_id):
            raise ValueError("physical action differs from issued observation identity")
        if message is None and not fallback:
            raise ValueError("missing physical action requires an engine-observed fallback cause")
        if message is not None and fallback:
            raise ValueError("received physical action cannot carry a missing-action fallback")
        cause = fallback
        if message is not None and message.action_name != self.expected:
            cause = "executor_mismatch"
        self.pending = None
        return AdmittedControl(
            request_id=issued.request_id,
            slot=self.slot,
            step=issued.step,
            directive_window_id=None if self.directive is None else self.directive.window_id,
            expected_action=self.expected,
            submitted_action=None if message is None else message.action_name,
            installed_action=self.expected if not cause else "noop",
            cause=cause,
        )

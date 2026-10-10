"""Exact issued engine tokens consumed by both copies of the ordinary executor."""

from mettagrid.policy.policy_env_interface import PolicyEnvInterface
from mettagrid.simulator.interface import AgentObservation, ObservationToken


def decode_observation(pei: PolicyEnvInterface, raw: list[list[int]], agent_id: int) -> AgentObservation:
    by_id = {spec.id: spec for spec in pei.obs_features}
    tokens = []
    for location, feature_id, value in raw:
        if feature_id == 255:
            if location != 255 or value != 255:
                raise ValueError("invalid empty observation token")
            continue
        tokens.append(ObservationToken(feature=by_id[feature_id], value=value, raw_token=(location, feature_id, value)))
    return AgentObservation(agent_id=agent_id, tokens=tokens)

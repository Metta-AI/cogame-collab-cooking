"""Bundled native macro container and ordinary scripted baselines."""

from __future__ import annotations

import os

from collab_cooking.coworld import lifecycle
from collab_cooking.coworld.plan_protocol import (
    DEFAULT_MODEL,
    NativeProfile,
    NativeRegistration,
    PlayerConfiguration,
    Registration,
    ScriptedRegistration,
)
from collab_cooking.coworld.player_socket import play


def registration(configuration: PlayerConfiguration) -> Registration:
    """Freeze policy inputs verbatim; these inputs never establish serving authority."""
    prompt = os.environ.get("PLAYER_PROMPT", "")
    if prompt:
        return NativeRegistration(
            slot=configuration.slot,
            prompt=prompt,
            profile=NativeProfile(
                model=os.environ.get("COWORLD_LLM_MODEL", DEFAULT_MODEL),
                max_tokens=int(os.environ.get("COWORLD_LLM_MAX_TOKENS", "900")),
                temperature=float(os.environ.get("COWORLD_LLM_TEMPERATURE", "1")),
            ),
        )
    return ScriptedRegistration.model_validate(
        {"slot": configuration.slot, "baseline": os.environ.get("PLAYER_SCRIPTED", "brigade")}
    )


def main() -> None:
    endpoint = os.environ["COWORLD_LLM_ENDPOINT"] if os.environ.get("PLAYER_PROMPT", "") else ""
    lifecycle.main_owned(play(os.environ["COWORLD_PLAYER_WS_URL"], registration, endpoint))


if __name__ == "__main__":
    main()

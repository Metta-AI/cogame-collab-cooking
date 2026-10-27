"""Private complete-episode export through the canonical Coworld SDK."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from coworld.decision_trajectory import DecisionAttempt, DecisionEvent, EpisodeEvent

from collab_cooking.coworld.server import write_uri


def write_capture(episode, results: dict) -> None:
    destination = os.environ.get("COGAME_SAVE_TRAJECTORY_URI")
    if not destination:
        return
    identity: dict[str, Any] = {
        "episode_id": os.environ["COWORLD_EPISODE_ID"],
        "game": os.environ.get("COWORLD_GAME_NAME", "collab_cooking"),
        "game_version": os.environ["COWORLD_GAME_VERSION"],
        "source_revision": os.environ["COWORLD_SOURCE_REVISION"],
        "image_digest": os.environ.get("COWORLD_GAME_IMAGE_DIGEST"),
    }
    events = []
    for row in episode._private_plans:
        request = row["request"]
        receipts = [] if episode.planner is None else episode.planner.evidence.get(request.decision_id, [])
        attempts = []
        selected = None
        for raw in receipts:
            receipt = dict(raw)
            receipt["accepted"] = bool(receipt.get("accepted") and row["status"] == "accepted")
            if receipt["accepted"]:
                selected = receipt["attempt_id"]
                receipt["parsed_action"] = row["action"]
            elif not receipt.get("rejection_reason"):
                receipt["rejection_reason"] = "response was not applied before episode/plan deadline"
            attempts.append(DecisionAttempt.model_validate(receipt))
        events.append(
            DecisionEvent(
                **identity,
                decision_id=request.decision_id,
                decision_index=len(events),
                seat=str(request.slot),
                observation=request.user,
                prompt=[{"role": "system", "content": request.system}, {"role": "user", "content": request.user}],
                attempts=attempts,
                selected_attempt_id=selected,
                executed_action=row["action"],
                action_status=row["status"],
                fallback_origin="player_scripted" if row["status"] == "fallback" else None,
            )
        )
    pending = episode.batch is not None and any(not future.done() for future in episode.batch.futures.values())
    status = "completed" if results["reason"] == "complete" and not pending else "truncated"
    events.append(
        EpisodeEvent(
            **identity,
            seed_family=f"{episode.config.layout}-{episode.config.seed}",
            status=status,
            outcome=results,
            participant_outcomes=results["scores"],
        )
    )
    body = "".join(event.model_dump_json() + "\n" for event in events).encode()
    if destination == os.environ.get("COGAME_SAVE_REPLAY_URI"):
        raise ValueError("private trajectory must have a separate destination from public replay")
    parsed = urlparse(destination)
    if parsed.scheme == "file":
        path = Path(unquote(parsed.path))
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
    else:
        write_uri(destination, body, method=os.environ.get("COGAME_SAVE_TRAJECTORY_METHOD", "PUT"))

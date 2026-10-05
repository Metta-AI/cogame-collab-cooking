"""Collect one complete ordinary game of source-controlled private-view macros.

No labels or split files are emitted. External content-bound review remains
required, and this logical-clock profile has no native serving authority.
"""

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from coworld.types import CoworldManifest

from collab_cooking.coworld import lifecycle
from collab_cooking.coworld.live_episode import EpisodeConfig
from collab_cooking.coworld.teacher import TEACHER_POLICY
from collab_cooking.coworld.teacher_collection import TeacherEpisode, seat_teachers
from collab_cooking.missions.kitchen import mission_for_config


async def collect(config: EpisodeConfig, directory: Path) -> dict:
    episode = TeacherEpisode.from_env(
        mission_for_config(config),
        config=config,
        tokens=[f"source-teacher-{slot}" for slot in range(4)],
        player_names=[TEACHER_POLICY] * 4,
        evidence_profile="scripted_teacher_logical_clock_v1",
    )
    assert isinstance(episode, TeacherEpisode)
    # Only the explicitly declared collector skips wall-clock sleeps. All
    # original physics, config clocks and 50-tick/10-logical-second gates remain.
    episode.step_seconds = 0
    actors = await seat_teachers(episode)

    def artifacts(results: bytes, replay: bytes) -> None:
        for name, data in (("results.json", results), ("replay.json", replay)):
            with os.fdopen(os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
                stream.write(data)

    episode.configure_artifacts(
        artifacts,
        generated_at=datetime.now(UTC).isoformat(),
    )
    try:
        await episode.run()
        if not episode.done or any(not actor.finished for actor in actors):
            raise ValueError("source teacher collection lacks whole-game actor completion")
        return episode.results()
    finally:
        if not await episode.close_owner(lifecycle.cleanup_deadline()):
            raise lifecycle.OwnershipUnsettled("teacher collection has an unresolved source writer")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("coworld_manifest_template.json"))
    parser.add_argument("--variant", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    version = f"source-{args.source_revision}"
    raw = json.loads(args.manifest.read_bytes())
    raw["game"]["version"] = version  # Runtime copy; the authoritative template stays versionless.
    manifest = CoworldManifest.model_validate(raw)
    variant = next(item for item in manifest.variants if item.id == args.variant)
    config = EpisodeConfig.from_mapping(variant.game_config | {"seed": args.seed})
    directory = args.output.resolve()
    directory.mkdir(mode=0o700, parents=True)
    os.environ.update(
        COGAME_SAVE_TRAJECTORY_URI=(directory / "trajectories.jsonl").as_uri(),
        COWORLD_EPISODE_ID=f"collab_cooking:{args.variant}:{args.seed}:source-teacher",
        COWORLD_SOURCE_REVISION=args.source_revision,
        COWORLD_GAME_VERSION=version,
    )
    os.environ.pop("COWORLD_GAME_IMAGE_DIGEST", None)
    result = lifecycle.main_owned(collect(config, directory))
    if result is None:
        raise RuntimeError("source teacher collection interrupted; private prefix retained")
    source = directory / "trajectories.jsonl"
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    summary = {
        "game": "collab_cooking",
        "game_version": version,
        "source_revision": args.source_revision,
        "variant": args.variant,
        "seed": args.seed,
        "seed_family": f"collab_cooking:{args.seed}",
        "policy": TEACHER_POLICY,
        "profile": "scripted_teacher_logical_clock_v1",
        "clock": "authored .2-second logical ticks; no wall sleeps or native latency",
        "trajectories_sha256": digest,
        "source_bytes": source.stat().st_size,
        "results": result,
        "authenticated_receipts": 0,
        "review_status": "unreviewed",
        "labels_published": False,
    }
    with os.fdopen(os.open(directory / "manifest.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as stream:
        json.dump(summary, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()

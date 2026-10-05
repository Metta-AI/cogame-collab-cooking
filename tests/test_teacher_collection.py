"""Ordinary private teacher/parser/controller/actual engine path, without model facts."""

import json

import pytest
from coworld.decision_trajectory import CompleteEpisode

from collab_cooking.coworld.live_episode import EpisodeConfig
from collab_cooking.coworld.plan_channel import PLAYER_FRAME
from collab_cooking.coworld.private_journal import RECORD, TeacherCall
from collab_cooking.coworld.teacher import TEACHER_POLICY
from collab_cooking.coworld.trajectory import finalize
from tools.collect_teachers import collect


@pytest.mark.asyncio
async def test_source_private_teacher_reaches_actual_cpp_and_common_canonical_producer(tmp_path, monkeypatch):
    output = tmp_path / "trajectory.jsonl"
    monkeypatch.setenv("COGAME_SAVE_TRAJECTORY_URI", output.as_uri())
    monkeypatch.setenv("COWORLD_EPISODE_ID", "diagnostic-source-teacher")
    monkeypatch.setenv("COWORLD_SOURCE_REVISION", "a" * 40)
    monkeypatch.setenv("COWORLD_GAME_VERSION", "diagnostic")
    result = await collect(EpisodeConfig(max_steps=12), tmp_path)
    complete = CompleteEpisode.model_validate_json(output.read_bytes())
    assert result["steps"] == 12
    assert complete.episode.status == "completed"
    assert complete.episode.outcome["evidence_profile"] == "scripted_teacher_logical_clock_v1"
    assert len(complete.decisions) == 4
    for decision in complete.decisions:
        assert decision.action_status == "accepted"
        assert decision.execution.control_encoding == "u8-u8"
        assert decision.execution.start_tick == 1 and decision.execution.end_tick == 12
        attempt = decision.attempts[0]
        assert attempt.origin == "teacher" and attempt.policy == TEACHER_POLICY
        assert not attempt.is_model_call
        assert attempt.response == json.dumps(decision.executed_action, separators=(",", ":"))
        assert attempt.parsed_action == decision.executed_action
        assert attempt.response_reader_joined is None and attempt.response_complete is None
    raw_path = output.with_name(output.name + ".evidence.partial.jsonl")
    records = [RECORD.validate_json(line) for line in raw_path.read_bytes().splitlines()]
    assert sum(record.kind == "engine_application" for record in records) == 12
    assert sum(record.kind == "private_tick" for record in records) == 48
    assert not any(record.kind in ("received_call", "granted_call") for record in records)
    assert "note" not in {
        key
        for tick in json.loads((tmp_path / "replay.json").read_bytes())["ticks"]
        for event in tick.get("ev", [])
        for key in event
    }
    call = next(record for record in records if isinstance(record, TeacherCall))
    with pytest.raises(ValueError):
        PLAYER_FRAME.validate_python(call.model_dump(mode="json"))

    # Forging both the submitted teacher text and installed macro still cannot
    # bypass the independently regenerated private-view source teacher.
    forged = tmp_path / "forged.private.jsonl"
    with forged.open("wb") as stream:
        for line in raw_path.read_bytes().splitlines():
            value = json.loads(line)
            if value["kind"] == "teacher_call" and value["attempt_id"] == str(call.attempt_id):
                response = json.loads(value["response"])
                response["note"] = "FORGED_PRIVATE_TEACHER"
                value["response"] = json.dumps(response, separators=(",", ":"))
            if value["kind"] == "installed_macro" and value["selected_attempt_id"] == str(call.attempt_id):
                value["directive"]["plan"]["note"] = "FORGED_PRIVATE_TEACHER"
            stream.write(json.dumps(value).encode() + b"\n")
    with pytest.raises(ValueError, match="scripted teacher differs"):
        finalize(forged, tmp_path / "forged.complete.jsonl")
    assert not (tmp_path / "forged.complete.jsonl").exists()


def test_actual_collector_cli_hydrates_a_valid_source_version_and_preserves_template(tmp_path):
    import hashlib
    import json
    import subprocess
    import sys
    from pathlib import Path

    from coworld.decision_trajectory import CompleteEpisode

    root = Path(__file__).resolve().parents[1]
    template = json.loads((root / "coworld_manifest_template.json").read_bytes())
    variant = template["variants"][0]
    variant["game_config"]["max_steps"] = 12  # Explicit short protocol diagnostic only.
    manifest = tmp_path / "author-template.json"
    manifest.write_text(json.dumps(template))
    before = hashlib.sha256(manifest.read_bytes()).hexdigest()
    output = tmp_path / "private-output"
    subprocess.run(
        [
            sys.executable,
            str(root / "tools/collect_teachers.py"),
            "--manifest",
            str(manifest),
            "--variant",
            variant["id"],
            "--seed",
            "1",
            "--source-revision",
            "a" * 40,
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == before
    assert "version" not in json.loads(manifest.read_bytes())["game"]
    episode = CompleteEpisode.model_validate_json((output / "trajectories.jsonl").read_bytes())
    assert episode.episode.game_version == "0.1.0+g" + "a" * 40
    assert episode.episode.source_revision == "a" * 40
    assert episode.episode.outcome["runtime_configuration"]["max_steps"] == 12
    assert episode.episode.outcome["evidence_profile"] == "scripted_teacher_logical_clock_v1"
    assert all(not attempt.is_model_call for decision in episode.decisions for attempt in decision.attempts)

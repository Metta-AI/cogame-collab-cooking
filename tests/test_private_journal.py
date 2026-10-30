"""Raw private evidence is a source-owned audit spool, not an approved corpus."""

import json
import os

import pytest

from collab_cooking.coworld.private_journal import RECORD, Journal, OwnershipState, SourceIdentity


def test_private_spool_keeps_unjoined_prefix_writable_and_rejects_replacement(tmp_path):
    path = tmp_path / "private.evidence.partial.jsonl"
    identity = SourceIdentity(episode_id="diagnostic", source_revision="a" * 40, game_version="diagnostic")
    journal = Journal(path, identity, {"seed": 1, "layout": "cramped"})
    journal.append(OwnershipState(joined=False, reason="owned engine writer has not joined"))
    assert not journal.stream.closed
    assert os.stat(path).st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        Journal(path, identity, {"seed": 1})
    journal.append(OwnershipState(joined=True, reason="actual owner settled later"))
    journal.close_joined()
    records = [RECORD.validate_json(line) for line in path.read_bytes().splitlines()]
    assert [record.kind for record in records] == ["source", "ownership", "ownership"]
    assert [json.loads(line)["joined"] for line in path.read_bytes().splitlines()[1:]] == [False, True]
    assert journal.stream.closed
    with pytest.raises(ValueError, match="already joined"):
        journal.append(OwnershipState(joined=True, reason="late duplicate"))


def test_private_capture_requires_actual_episode_id_and_absolute_file_uri(tmp_path, monkeypatch):
    monkeypatch.delenv("COGAME_SAVE_TRAJECTORY_URI", raising=False)
    assert Journal.from_environment({"seed": 1}) is None
    monkeypatch.setenv("COGAME_SAVE_TRAJECTORY_URI", "https://example.com/private")
    with pytest.raises(ValueError, match="absolute file URI"):
        Journal.from_environment({"seed": 1})
    monkeypatch.setenv("COGAME_SAVE_TRAJECTORY_URI", (tmp_path / "source.jsonl").as_uri())
    monkeypatch.delenv("COWORLD_EPISODE_ID", raising=False)
    with pytest.raises(KeyError, match="COWORLD_EPISODE_ID"):
        Journal.from_environment({"seed": 1})


def test_missing_immutable_provenance_remains_unqualified_raw_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("COGAME_SAVE_TRAJECTORY_URI", (tmp_path / "unknown.jsonl").as_uri())
    monkeypatch.setenv("COWORLD_EPISODE_ID", "actual-preview-episode")
    for name in ("COWORLD_SOURCE_REVISION", "COWORLD_GAME_VERSION", "COWORLD_GAME_IMAGE_DIGEST"):
        monkeypatch.delenv(name, raising=False)
    journal = Journal.from_environment({"seed": 7})
    assert journal.identity.source_revision is None
    assert journal.identity.image_digest is None
    assert journal.identity.game_version is None
    journal.close_joined()

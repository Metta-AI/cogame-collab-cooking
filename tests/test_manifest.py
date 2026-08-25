"""The manifest's cross-checks.

`num_agents` is declared in eight variants, the certification fixture, the
config schema and `SMOKE_SEATS`. The ladder schedules zero episodes when any
one of them drifts, so all of them are asserted here against the COMMITTED
file, not against the generator's output.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from collab_cooking.kitchens.layouts import LAYOUT_NAMES

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "coworld_manifest_template.json").read_text(encoding="utf-8"))
NUM_AGENTS = 4
GAME_NAME = "collab_cooking"
IMAGE = "{{COLLAB_COOKING_IMAGE}}"


def test_num_agents_is_four_in_every_variant() -> None:
    assert len(MANIFEST["variants"]) == 8
    assert {variant["id"] for variant in MANIFEST["variants"]} == set(LAYOUT_NAMES)
    for variant in MANIFEST["variants"]:
        assert variant["game_config"]["num_agents"] == NUM_AGENTS, variant["id"]
        assert variant["game_config"]["layout"] == variant["id"]
        assert variant["description"], "every variant needs a description"
        assert variant["game_config"]["max_steps"] == 900
        assert len(variant["game_config"]["players"]) == NUM_AGENTS


def test_num_agents_is_four_in_the_certification_fixture() -> None:
    cert = MANIFEST["certification"]
    assert cert["game_config"]["num_agents"] == NUM_AGENTS
    assert len(cert["game_config"]["players"]) == NUM_AGENTS
    assert len(cert["game_config"]["tokens"]) == NUM_AGENTS
    assert len(cert["players"]) == NUM_AGENTS


def test_every_declared_bundled_player_is_seated_at_least_once() -> None:
    declared = {entry["id"] for entry in MANIFEST["player"]}
    seated = {entry["player_id"] for entry in MANIFEST["certification"]["players"]}
    assert declared == seated, f"players_missing would fire on {declared - seated}"
    assert len(declared) == 4


def test_the_certification_fixture_is_cross_play_by_construction() -> None:
    by_id = {entry["id"]: entry for entry in MANIFEST["player"]}
    kinds = [
        "prompt" if "PLAYER_PROMPT" in by_id[seat["player_id"]]["env"] else "scripted"
        for seat in MANIFEST["certification"]["players"]
    ]
    assert kinds.count("prompt") == 1
    assert kinds.count("scripted") == 3


def test_smoke_seats_agrees_with_the_manifest() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'SMOKE_SEATS: "4"' in ci
    smoke = (ROOT / "tools" / "ci" / "docker_smoke.sh").read_text(encoding="utf-8")
    assert 'seats_expected="${SMOKE_SEATS:-4}"' in smoke


def test_config_schema_bounds_num_agents_and_every_array() -> None:
    schema = MANIFEST["game"]["config_schema"]
    assert schema["additionalProperties"] is False
    properties = schema["properties"]
    assert properties["num_agents"]["minimum"] == NUM_AGENTS
    assert properties["num_agents"]["maximum"] == NUM_AGENTS
    for name, prop in properties.items():
        if prop.get("type") == "array":
            assert "minItems" in prop and "maxItems" in prop, name
            assert prop["minItems"] == prop["maxItems"] == NUM_AGENTS, name
    assert properties["layout"]["enum"] == list(LAYOUT_NAMES)


def test_results_schema_covers_every_key_and_bounds_the_reason() -> None:
    schema = MANIFEST["game"]["results_schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["reason"]["enum"] == ["complete", "deadline", "no_players"]
    from collab_cooking.coworld.results import build_results

    sample = build_results(
        reason="complete", layout="cramped", steps=480, delivered=[1, 2, 3, 4],
        served_by_recipe={"salad": 1, "soup": 2, "fries": 7}, orders_arrived=26,
        orders_expired=16, burned={"pot": 1, "fryer": 2}, blocked_moves=[1, 2, 3, 4],
        handoffs=[0, 0, 0, 0], names=["a", "b", "c", "d"],
        aliases=["Cog-A", "Cog-B", "Cog-C", "Cog-D"],
        seat_kinds=["prompt", "scripted:brigade", "scripted:passer", "scripted:courier"],
        disconnected=[False] * 4, fallbacks=[0] * 4, llm_requests=0,
    )
    assert set(sample) == set(schema["properties"]), "results.json and its schema disagree"
    assert set(schema["required"]) == set(sample)
    for name, prop in schema["properties"].items():
        if prop.get("type") == "array":
            assert prop["minItems"] == prop["maxItems"] == NUM_AGENTS, name


def test_protocols_carry_both_player_and_global_as_objects() -> None:
    protocols = MANIFEST["game"]["protocols"]
    assert set(protocols) == {"player", "global"}
    for name, entry in protocols.items():
        assert isinstance(entry, dict), name
        assert entry["type"] == "text"
        assert entry["value"].strip(), name


def test_docs_readme_is_inline_and_byte_identical_to_the_readme() -> None:
    docs = MANIFEST["game"]["docs"]
    assert docs["readme"]["type"] == "text"
    assert "uri" not in docs["readme"]
    assert docs["readme"]["value"] == (ROOT / "README.md").read_text(encoding="utf-8")
    assert [page["id"] for page in docs["pages"]] == ["rules", "kitchens", "policies", "protocol"]
    for page in docs["pages"]:
        assert page["title"] and page["content"]["type"] == "text"
        assert page["content"]["value"].strip()


def test_the_replay_viewer_is_the_static_bundle() -> None:
    # Under `game`, which is the ONLY place the coworld package reads it from
    # (bundle.py gates the build hook on `manifest.game.replay_viewer`, and
    # upload.py gates the bundle upload on `manifest["game"]["replay_viewer"]`).
    # The manifest model forbids extra keys, so a top-level declaration is
    # rejected outright by `coworld build`.
    assert MANIFEST["game"]["replay_viewer"] == {"bundle": "static-replay-viewer"}
    assert "replay_viewer" not in MANIFEST
    raw = (ROOT / "src" / "collab_cooking" / "coworld" / "server.py").read_text(encoding="utf-8")
    # Route declarations only: the module docstring says what was deleted, and
    # saying so is not a route.
    server = "\n".join(
        line for line in raw.splitlines() if not line.lstrip().startswith(("#", "*", "``"))
    ).split('"""', 2)[-1]
    for gone in ("/client/replay", "create_replay_app", "COGAME_REPLAY_SERVER",
                 "COGAME_LOAD_REPLAY_URI", '@app.websocket("/replay")'):
        assert gone not in server, f"{gone} is still live in server.py"


def test_the_manifest_carries_only_keys_the_coworld_schema_admits() -> None:
    """`coworld build` validates the template with an `extra="forbid"` model
    before it touches docker, so a key in the wrong object fails the first step
    of the release. `tools/ci/check_manifest_loads.py` runs the real loader in
    CI; these are the four the loader rejected on 2026-08-25."""
    assert set(MANIFEST) <= {
        "$schema", "tags", "game", "player", "reporter", "commissioner", "grader",
        "diagnoser", "optimizer", "variants", "certification", "players_per_user",
        "episode_timeout_minutes",
    }, "the top level of the manifest model is additionalProperties: false"
    game = MANIFEST["game"]
    assert "display_name" not in game, "game has no display_name field"
    # game.version is set by `coworld build --version`; a template that carries
    # it is refused outright.
    assert "version" not in game
    assert game["owner"], "game.owner is required"


def test_the_runnable_carries_the_secret_uri_in_the_game_name_namespace() -> None:
    runnable = MANIFEST["game"]["runnable"]
    assert runnable["type"] == "game"
    assert runnable["image"] == IMAGE
    assert runnable["run"] == ["/bin/collab-cooking"]
    assert runnable["env"]["ANTHROPIC_API_KEY_URI"] == (
        f"secret://coworld/{GAME_NAME}/anthropic_api_key"
    )
    # The namespace is game.name, not the slug -- they differ here.
    assert MANIFEST["game"]["name"] == GAME_NAME
    assert GAME_NAME != "collab-cooking"


def test_the_upload_contract_fields_are_present() -> None:
    assert MANIFEST["$schema"]
    assert len(MANIFEST["tags"]) >= 3
    assert MANIFEST["episode_timeout_minutes"] == 20
    for entry in MANIFEST["player"]:
        assert entry["type"] == "player"
        assert entry["id"] and entry["name"] and entry["description"]
        assert entry["image"] == IMAGE
        assert entry["run"] == ["/bin/collab-cooking-player"]


def test_the_image_placeholder_is_derived_from_the_compose_service() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "collab_cooking:" in compose
    assert "image: coworld-collab-cooking:latest" in compose
    assert "platform: linux/amd64" in compose
    assert "network: host" in compose
    # service `collab_cooking` -> {{COLLAB_COOKING_IMAGE}}
    assert IMAGE == "{{" + "collab_cooking".upper() + "_IMAGE}}"
    assert json.dumps(MANIFEST).count(IMAGE) == 5


@pytest.mark.parametrize(
    "path",
    [".github/workflows/ci.yml", ".github/workflows/coworld-release.yml",
     ".github/workflows/coworld-submit.yml", "tools/ci/docker_smoke.sh",
     "tools/ci/policies.json"],
)
def test_no_unsubstituted_scaffold_placeholders(path: str) -> None:
    text = (ROOT / path).read_text(encoding="utf-8")
    residue = set(re.findall(r"<[A-Za-z_][A-Za-z0-9_]*>", text))
    # <run_id> and <name> are runtime values in comments, not substitutions.
    assert residue <= {"<run_id>", "<name>", "<sha>", "<cow_id>"}, residue
    for placeholder in ("<slug>", "<IMAGE>", "<SEATS>"):
        assert placeholder not in text, f"{placeholder} was never substituted in {path}"


def test_the_manifest_loader_check_runs_the_version_the_release_pins() -> None:
    """The loader gate is only evidence if it validates against the coworld the
    release actually runs, so the two pins must not drift apart."""
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github" / "workflows" / "coworld-release.yml").read_text(encoding="utf-8")
    assert "tools/ci/check_manifest_loads.py" in ci
    gate = re.search(r'pip install --quiet "coworld==([0-9.]+)"', ci)
    pinned = re.search(r'COWORLD_PKG: "coworld\[auth\]==([0-9.]+)"', release)
    assert gate and pinned, "both pins must be greppable"
    assert gate.group(1) == pinned.group(1), (
        f"ci.yml validates the manifest with coworld {gate.group(1)} but the release "
        f"runs {pinned.group(1)}"
    )


def test_the_hooks_ci_needs_are_executable() -> None:
    for path in ("tools/build_replay_viewer.sh", "tools/ci/docker_smoke.sh"):
        mode = (ROOT / path).stat().st_mode
        assert mode & 0o111, f"{path} must be committed executable (mode 100755)"

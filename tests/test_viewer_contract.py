"""The viewer contract, checked without a browser.

`ci.yml`'s wasm-viewer job actually opens the bundle in headless chromium;
these are the static checks that would have caught cogame-lantern's split
bootstrap (a shell from one starter, link flags from another) before the
browser ever ran, plus the DOM contract the game block owes the shared chrome.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from collab_cooking.coworld.replay import EVENT_NAMES

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "client" / "replay_broadcast.html").read_text(encoding="utf-8")
GAME_JS = (ROOT / "client" / "parts" / "game.js").read_text(encoding="utf-8")
GAME_CSS = (ROOT / "client" / "parts" / "game.css").read_text(encoding="utf-8")
CHROME = (ROOT / "client" / "chrome_common.js").read_text(encoding="utf-8")
CONFIG_NIMS = (ROOT / "replay-viewer" / "config.nims").read_text(encoding="utf-8")
WASM_ENTRY = (ROOT / "replay-viewer" / "collab_cooking_replay.nim").read_text(encoding="utf-8")
STATIC_JS = (ROOT / "replay-viewer" / "static_replay.js").read_text(encoding="utf-8")
WORKER_JS = (ROOT / "replay-viewer" / "static_replay_worker.js").read_text(encoding="utf-8")

BEAT_KINDS = ["serve", "burn", "expire", "jam", "plan", "end"]
# Every id chrome_common.js resolves by getElementById. The page must keep all
# of them, including the ones the game block leaves empty.
CHROME_IDS = [
    "btn-loop", "btn-play", "btn-skip", "btn-spoilers", "clock", "clock-caption",
    "clock-time", "ffwd-chip", "ffwd-mini", "lulls", "momentum", "scrub",
    "scrub-fill", "scrub-head", "scrub-win", "speedchips", "tick-clock",
    "transport", "win-chip",
]
REMOVED_IDS = [
    "fpv", "fpv-canvas", "fpv-cap", "fpv-gear", "fpv-grip", "fpv-hp", "fpv-hud",
    "fpv-map", "fpv-map-canvas", "fpv-name", "lockerroom", "lk-art", "lk-bg",
    "lk-cap", "lk-sprites", "killfeed", "povBadge", "mmwarn", "viewpanel",
    "zoombar", "zoom-in", "zoom-out", "zoom-read", "zoom-slider", "minimap",
    "minimap-canvas",
]
CC_EXPORTS = [
    "cc_load_replay", "cc_frame", "cc_input", "cc_packet_ptr", "cc_packet_len",
    "cc_error_ptr", "cc_error_len", "cc_stage_ptr", "cc_stage_len",
]


def page_ids() -> set[str]:
    return set(re.findall(r'id="([A-Za-z0-9_-]+)"', PAGE))


def test_the_server_can_emit_exactly_the_documented_event_vocabulary() -> None:
    # The Nim module validates every event name against its own copy of the
    # list; the two must be the same list.
    block = WASM_ENTRY.split("EventNames = [", 1)[1].split("]", 1)[0]
    viewer_names = re.findall(r'"([a-z_]+)"', block)
    assert viewer_names == list(EVENT_NAMES)


def test_every_scrubber_beat_kind_has_a_css_rule() -> None:
    for kind in BEAT_KINDS:
        assert f".beat-marker.{kind}" in GAME_CSS, kind
    # And nothing else is ever put on the scrubber.
    emitted = set(re.findall(r'kind:\s*"([a-z]+)",\s*label', WASM_ENTRY))
    assert emitted <= set(BEAT_KINDS), f"undressed beat kinds: {emitted - set(BEAT_KINDS)}"


def test_scrubber_beats_are_labelled_clickable_buttons() -> None:
    assert "createElement('button')" in GAME_JS
    assert "setAttribute('aria-label'" in GAME_JS
    assert "button.title = beat.label" in GAME_JS
    assert "ccSeek(beat.t" in GAME_JS


def test_the_game_block_declares_no_function_colliding_with_a_chrome_alias() -> None:
    # `var markBeat = C.markBeat` is hoisted-over by `function markBeat(){}`,
    # which is how tandem's beats became unlabeled dead divs.
    aliases = set(re.findall(r"^\s*var (\w+) = C\.", GAME_JS, re.M))
    aliases |= set(re.findall(r"^\s*(\w+):\s*\w+[,}]", CHROME, re.M))
    declared = set(re.findall(r"^\s*function (\w+)\s*\(", GAME_JS, re.M))
    declared |= set(re.findall(r"^\s*var (\w+) = function", GAME_JS, re.M))
    collisions = declared & aliases
    assert not collisions, f"the game block shadows ChromeCommon names: {collisions}"
    # The four the design names are all cc-prefixed.
    for name in ("ccDishTicker", "ccHeatToggle", "ccSayBar", "ccSeatPlates"):
        assert f"var {name} = function" in GAME_JS


def test_the_page_keeps_every_id_chrome_common_resolves() -> None:
    ids = page_ids()
    missing = [name for name in CHROME_IDS if name not in ids]
    assert not missing, f"chrome_common.js would throw on: {missing}"


def test_the_page_dropped_the_ctf_specific_elements() -> None:
    ids = page_ids()
    present = [name for name in REMOVED_IDS if name in ids]
    assert not present, f"ctf elements that should be gone: {present}"
    assert "attachMinimap(" not in GAME_JS, "the zoom bar and minimap are dropped entirely"


def test_the_page_keeps_the_scorebug_plates_and_the_appended_readouts() -> None:
    ids = page_ids()
    for name in ("scorebug", "plates-l", "plates-r", "board", "stage", "viewport",
                 "chrome", "bannerlane", "endcard", "status",
                 "dishticker", "saybar", "feed", "heatbtn"):
        assert name in ids, name
    assert "collab-cooking additions to the inherited coworld-ctf chrome" in PAGE


def test_relayout_owns_the_band_variables_and_the_game_block_only_reads_them() -> None:
    assert "root.style.setProperty('--band'" in GAME_JS
    assert "root.style.setProperty('--hudscale'" in GAME_JS
    body = GAME_JS.split("function relayout()", 1)[1]
    outside = "\n".join(
        line for line in GAME_JS.split("function relayout()", 1)[0].splitlines()
        if not line.lstrip().startswith("//")
    )
    assert "--hudscale" not in outside, "only relayout() may write --hudscale"
    assert "setProperty('--band'" in body
    # Nothing is overlaid inside the transport band: the feed stops above it.
    assert "bottom: calc(var(--band, 0px)" in GAME_CSS
    assert "#dishticker" in GAME_CSS and "top: var(--sb, 0px)" in GAME_CSS


def test_every_seek_dismisses_the_endcard() -> None:
    seek = GAME_JS.split("function ccSeek(", 1)[1].split("\n  }", 1)[0]
    assert "classList.remove('on')" in seek
    assert "ccSend('s:'" in seek
    # And every transport control that moves the playhead goes through ccSeek.
    for control in ("btn-restart", "btn-back", "btn-fwd"):
        line = [row for row in GAME_JS.splitlines() if f"bind('{control}'" in row][0]
        assert "ccSeek(" in line, control


def test_plate_css_survives_the_360px_featured_match_iframe() -> None:
    plate = GAME_CSS.split(".plate-name {", 1)[1].split("}", 1)[0]
    assert "flex: 1 1 auto" in plate
    assert "min-width: 3.2em" in plate
    assert "@media (max-width: 640px)" in GAME_CSS


def test_the_wasm_entry_the_link_flags_and_the_js_name_the_same_symbols() -> None:
    """The static check that would have caught cogame-lantern's split
    bootstrap: a shell from one starter and link flags from another."""
    for symbol in CC_EXPORTS:
        assert f'exportc: "{symbol}"' in WASM_ENTRY, f"{symbol} is not exported from Nim"
        assert f"_{symbol}" in CONFIG_NIMS, f"{symbol} is not in EXPORTED_FUNCTIONS"
    used = set(re.findall(r"Module\._(\w+)\(", WORKER_JS))
    assert used <= set(CC_EXPORTS) | {"malloc", "free"}, f"the worker calls unexported {used}"
    for symbol in ("cc_load_replay", "cc_frame", "cc_packet_ptr", "cc_packet_len", "cc_input"):
        assert f"Module._{symbol}(" in WORKER_JS
    assert "ctf_" not in WORKER_JS and "ctf_" not in CONFIG_NIMS.split("# ")[0]
    # MODULARIZE / EXPORT_NAME would need a factory call the worker never makes.
    assert "MODULARIZE" not in CONFIG_NIMS
    assert "EXPORT_NAME" not in CONFIG_NIMS
    assert "onRuntimeInitialized" in WORKER_JS
    assert "importScripts('./broadcast_core.js', './collab_cooking_replay.js')" in WORKER_JS
    assert "collab_cooking_replay.js" in CONFIG_NIMS


def test_the_shell_reports_both_success_and_failure() -> None:
    assert "setAttribute('data-replay-loaded', 'true')" in STATIC_JS
    assert "setAttribute('data-replay-error'" in STATIC_JS
    # The bridge `ready` is posted from INSIDE the branch that sets
    # data-replay-loaded, never on rAF at the call site.
    branch = STATIC_JS.split("message.type === 'loaded'", 1)[1].split("} else if", 1)[0]
    assert "data-replay-loaded" in branch and "postReplayBridge({ type: 'ready' })" in branch
    assert "src: 'coworld-replay'" in STATIC_JS


def test_the_worker_name_and_the_page_hook_are_ours() -> None:
    assert "name: 'collab-cooking-static-replay'" in STATIC_JS
    assert "window.CcStaticReplay" in STATIC_JS
    assert "window.CcStaticReplay" in GAME_JS


def test_the_build_hook_asserts_every_file_the_page_loads() -> None:
    hook = (ROOT / "tools" / "build_replay_viewer.sh").read_text(encoding="utf-8")
    for name in (
        "index.html", "chrome_common.js", "broadcast_core.js", "static_replay.js",
        "static_replay_worker.js", "collab_cooking_replay.js",
        "collab_cooking_replay.wasm", "collab_cooking_replay.data",
    ):
        assert name in hook, name
    # mkdir -p the output parent BEFORE the containment check.
    before = hook.split('output_parent=', 1)[0]
    assert 'mkdir -p "$(dirname "${requested_output}")"' in before


def test_the_chrome_is_byte_identical_to_the_starter() -> None:
    starter = Path("/workspace/starters/coworld-ctf/client")
    if not starter.exists():
        pytest.skip("the coworld-ctf starter is not mounted here")
    for name in ("chrome_common.js", "broadcast_core.js"):
        assert (ROOT / "client" / name).read_bytes() == (starter / name).read_bytes()


def test_the_state_contract_the_page_reads_is_the_one_the_module_emits() -> None:
    emitted = set(re.findall(r'"(\w+)"', WASM_ENTRY))
    emitted |= set(re.findall(r'doc\["(\w+)"\]', WASM_ENTRY))
    for key in ("tick", "ticks", "layout", "phase", "dishes", "live", "expiring",
                "expired", "burned", "seats", "ticker", "heat", "feed", "beats",
                "final", "reason"):
        assert key in emitted, key
    for key in ("s.dishes", "s.ticker", "s.seats", "s.beats", "s.feed", "s.final",
                "s.heatOn", "s.live", "s.expiring"):
        assert key in GAME_JS, key
    assert '"final"' in WASM_ENTRY or 'doc["final"]' in WASM_ENTRY


def test_the_policies_file_is_the_designed_set() -> None:
    policies = json.loads((ROOT / "tools" / "ci" / "policies.json").read_text(encoding="utf-8"))
    assert [entry["name"] for entry in policies] == [
        "collab-cooking-expo", "collab-cooking-linecook",
        "collab-cooking-brigade", "collab-cooking-passer",
    ]
    assert "PLAYER_PROMPT" in policies[0]["env"] and "PLAYER_PROMPT" in policies[1]["env"]
    assert policies[1]["player"] == "ply_bac48eb1-662e-44f8-973d-f3e016dccf5d"
    assert policies[2]["env"] == {"PLAYER_SCRIPTED": "brigade"}
    assert policies[3]["env"] == {"PLAYER_SCRIPTED": "passer"}
    # The LLM is game-side, so USE_BEDROCK buys a player pod nothing.
    assert all("USE_BEDROCK" not in entry["env"] for entry in policies)
    assert all(entry["run"] == "/bin/collab-cooking-player" for entry in policies)

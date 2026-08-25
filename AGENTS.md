# AGENTS.md

Guidance for coding agents working in `cogame-collab-cooking`.

## Read first

1. [`docs/plans/2026-08-25-collab-cooking-design.md`](docs/plans/2026-08-25-collab-cooking-design.md)
   is the design note this repo implements. It is authoritative: if the code and the note
   disagree, the note is what was reviewed.
2. [`docs/rules.md`](docs/rules.md) for the game, [`docs/policies.md`](docs/policies.md) for the
   policy interface, [`docs/protocol.md`](docs/protocol.md) for the wire.

## Provenance

Two starters, and nothing is spliced between them:

* the **kitchen** (`src/collab_cooking/game/`, `src/collab_cooking/agent/brain/`) is
  `Metta-AI/coworld-overcogged`'s mettagrid game and scripted brain;
* the **viewer** (`replay-viewer/`, `client/`) is `Metta-AI/coworld-ctf` -- the wasm entry, the
  emscripten link flags, the JS bootstrap, `chrome_common.js` and `broadcast_core.js` all come
  from there and only from there. The flags and the bootstrap are a matched pair; a mixture
  hangs on "Loading replay..." forever.

`client/replay_broadcast.html` is generated: `python3 tools/build_broadcast_page.py <ctf-checkout>`
takes ctf's page, deletes the elements the design note lists, and appends `client/parts/*`. Edit
the parts, not the generated page.

`coworld_manifest_template.json` is generated too (`python3 tools/build_manifest.py`), because
`num_agents` is declared in eleven places and they must not drift. `tests/test_manifest.py`
asserts the invariants on the committed file, so a hand edit is still checked.

## Running things

```bash
pip install -e ".[standalone]" pytest
python -m pytest tests/ -v
```

The wasm viewer needs Nim 2.2.4 + emsdk 4.0.15:

```bash
nimby use 2.2.4 && nimby --global sync nimby.lock
nim c -d:emscripten replay-viewer/collab_cooking_replay.nim
```

`tools/replay_probe.nim` renders a replay natively (no browser, no emsdk) and writes a PNG of
one frame -- the fastest way to see whether a board change looks right:

```bash
nim c -r --mm:arc tools/replay_probe.nim dist/smoke/replay.json
```

## Non-negotiables

1. **Degrade, never hang.** Every wait is bounded and every failure has a fallback that keeps
   play moving. The game never exits non-zero on a player-side problem.
2. **Rune-boundary truncation on every recorded string.** A byte cut mid-rune renders in a
   browser and fails a strict JSON parse.
3. **One parallel batch per plan turn.** This is a simultaneous game; sequential seat calls blow
   the wall-clock budget.
4. **Two name spaces.** Cogs see `Cog-A`...`Cog-D` only; real policy names are spectator-side.
5. **The replay is a static bundle, never a pod.** There is no `/client/replay` route and there
   will not be one.
6. **`num_agents` is 4 everywhere.** Variants, cert fixture, config schema, `SMOKE_SEATS`.

# Private macro training evidence

The ordinary hosted player uses the native Messages sidecar and the frozen
`collab-cooking.player.v2` protocol. The game issues simultaneous private plan
windows, shared rate grants and retry grants. Players own inference and parsing.
The game independently parses selected received text and mirrors the shipped
controller before applying physical controls. An installed directive becomes an
accepted label only after matching controls reach the actual engine buffers.

The canonical producer imports `coworld.decision_trajectory` directly and
requires Coworld >=0.1.57. That stable release is a deployment prerequisite.
A private source-pinned candidate wheel can validate development dependencies;
it does not establish a published dependency or a deployed runtime.

Private raw journals retain issued observations, grants, received native attempts,
all physical applications, outcomes and ownership facts. Canonical files retain
one macro decision per private window and reference the hash-bound physical
journal. Raw HTTP body bytes and headers remain private. Public replays exclude
private strategy, notes, observations and inference evidence.

Publication requires observed engine, player, reader and writer joins. An
unresolved owner leaves its private prefix available and withholds public
completion. Canonical finalization runs in an owned process; its candidate is
admitted only after actual process exit. Artifact HTTP has an absolute budget.

## Source-controlled teacher collection

Run one complete ordinary game per private output directory:

```bash
python tools/collect_teachers.py --variant open-kitchen --seed 1 \
  --source-revision <tested-commit> --output <private-directory>
```

Use an actual variant ID from `coworld_manifest_template.json`. The author
manifest stays versionless; collection adds the source version to a runtime copy.

The explicit `scripted_teacher_logical_clock_v1` profile runs all ordinary
900 physical ticks. It preserves the authored 0.2-second logical tick and the
50-tick/10-second plan schedule, while omitting wall-clock sleeps. Every teacher
reads only its ordinary private window and passes the ordinary macro parser.
All four physical actors use the shipped controller and actual game admission.
This profile provides no native serving receipts or wall-clock inference proof.

Collection emits unreviewed complete episodes, results, replay and a hash manifest.
It emits no labels or train/validation split files. Training export requires an
external content-bound `TeacherLabelReview` and independent source/engine replay.
Keep all eight layouts and whole seed families together when assigning splits.
Native model labels additionally require authenticated platform receipts and
intended checkpoint context through the Metta hosted importer.

`tools/training_bridge.py --research-physical` is a separate numeric physical
research surface. Its catalog does not qualify native language macro parity.
No source collection, fixture or certification proves trained model strength.

# Numeric training

`tools/training_bridge.py` runs the published kitchen simulator through Metta's
persistent JSONL GameBridge protocol. Each decision uses the hosted player's
private physical observation and one of its five action names. Four
seats decide from one frozen tick, then the game applies all actions and steps
once. The game owns movement, station rules, ticket timing, scores, and replay.

Create the local environment, then select any of the eight manifest variant IDs:

```sh
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e '.[test]'
.venv/bin/python tools/training_bridge.py --research-physical --variant cramped
```

The bridge reads `reset`, `encode`, `teacher`, and `step` JSON lines on stdin.
`reset` requires four players and a string seed. `encode` returns 1,510 numeric
values and five unmasked action objects. Values contain a seat one-hot vector,
tick and horizon, public scores, and the 500 observation triples sent to that
seat by the game. A typed candidate question exposes the same five actions
for external choice policies and includes the player-visible feature
table. The teacher is the bundled `KitchenBrain` player with the
normal brigade, brigade, passer, and courier roster. It sees only its own
player observation. A terminal response returns the game's team-dish score
with the published per-seat delivery tie-break. Use `--steps N` for a shorter
curriculum; the default is the variant's full 900 ticks.

From a Metta checkout, set the GameBridge command to this repository's
`.venv/bin/python` and `tools/training_bridge.py`, plus `--research-physical`, `--variant`, and a
manifest variant ID. The numeric codec uses `observation_size=1510`,
`action_sizes=[5]`, `players=4`, and `max_decisions>=3600` with Metta's
`recipes.external.coworld` recipe. Keep the game package and manifest available
to the bridge subprocess. Native Puffer training requires CUDA.

The local verification command is:

```sh
.venv/bin/python -m pytest tests/test_training_bridge.py -q
```

Historical numeric-bridge checks completed eight 900-tick episodes with 3,600
accepted teacher actions each. A historical `DecisionEnvironment` episode also
finished with 1,510 values, five actions, and teacher targets. These historical checks
cover their original bridge source and game score. Current native runtime
and macro teacher evidence require separate source-pinned gates. They do not measure a trained
policy's quality. This bridge trains per-tick numeric actions. The hosted
prompt-plan policies choose shift jobs while their ordinary players
submit per-tick actions through the same seat observation/action protocol.

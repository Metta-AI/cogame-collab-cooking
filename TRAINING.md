# Numeric training

`tools/training_bridge.py` runs the published kitchen simulator through Metta's
persistent JSONL GameBridge protocol. Each decision uses the hosted player's
`collab-cooking.player.v1` observation and one of its five action names. Four
seats decide from one frozen tick, then the game applies all actions and steps
once. The game owns movement, station rules, ticket timing, scores, and replay.

Create the local environment, then select any of the eight manifest variant IDs:

```sh
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e '.[test]'
.venv/bin/python tools/training_bridge.py --variant cramped
```

The bridge reads `reset`, `encode`, `teacher`, and `step` JSON lines on stdin.
`reset` requires four players and a string seed. `encode` returns 1,510 numeric
values and five unmasked action objects. Values contain a seat one-hot vector,
tick and horizon, public scores, and the 500 observation triples sent to that
seat by the game. A typed candidate question exposes the same five actions
for optional Jev choice decisions and includes the player-visible feature
table. The teacher is the bundled `KitchenBrain` player with the
normal brigade, brigade, passer, and courier roster. It sees only its own
player observation. A terminal response returns the game's team-dish score
with the published per-seat delivery tie-break. Use `--steps N` for a shorter
curriculum; the default is the variant's full 900 ticks.

From a Metta checkout, set the GameBridge command to this repository's
`.venv/bin/python` and `tools/training_bridge.py`, plus `--variant` and a
manifest variant ID. The numeric codec uses `observation_size=1510`,
`actions=5`, `players=4`, and `max_decisions>=3600` with Metta's
`recipes.external.coworld` or `recipes.external.coworld_metta_rl` recipe.
Keep the game package and manifest available to the bridge subprocess.

The local verification command is:

```sh
.venv/bin/python -m pytest tests/test_training_bridge.py -q
```

All eight variants completed a full 900-tick GameBridge episode with 3,600
accepted teacher actions each. A full `DecisionEnvironment` episode also
finished with 1,510 values, five actions, and teacher targets. These checks
verify the training protocol and game score. They do not measure a trained
policy's quality. This bridge trains per-tick numeric actions; the hosted
prompt-plan and radio policies remain separate. The typed choice can exercise
Jev over the ordinary action schema but does not add a hosted Jev player.
